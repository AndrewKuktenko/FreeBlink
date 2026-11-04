"""Clip descriptions with Claude: a few frames in, a short structured description out."""

import base64
import copy
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import anthropic

from . import config
from .media import downscale, frames_and_duration

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You caption motion clips from a home security camera. The homeowner reads your \
caption as a phone notification, so it must be short, concrete and correct.

You get frames from one short clip, in time order, and the camera's name.

Rules:
- Say what happens: who or what appears, what they do, where they go. One or two short sentences, \
at most 30 words in total. Never mention frames or images; describe the scene as one event.
- Describe only what is clearly visible. If something is too small, dark or blurry to identify, \
say "someone" or "something" instead of guessing. Night footage is often grainy grayscale.
- Mention a detail (a package, a dog on a leash, a car door opening) only when it matters to what \
happens. Skip clothing unless it is the only way to tell people apart.
- Background (parked cars, plants, weather, a flooded street, the house itself) stays out of the \
caption unless nothing else happens.
- If nothing moves except light, shadows, weather or plants, say so plainly, for example \
"No one is visible; only leaves move in the wind."
- No speculation about identity or intent. Never guess who someone is from how they look.
- Sometimes the input includes a note from FreeBlink's face matcher, which runs separately and names \
household members the homeowner enrolled. When it does, refer to the person it most plausibly means \
by that name (e.g. "Anna arrives home"). If several people are visible and you can't tell which one \
was matched, say "Anna and someone else" or similar; never attach a name to a person by guessing."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "2-5 word headline, sentence case, no period. E.g. 'Person at the door', "
                           "'Car pulls into driveway', 'Dog in the yard', 'No activity'.",
        },
        "description": {"type": "string", "description": "1-2 sentences, at most 30 words."},
        "subjects": {
            "type": "array",
            "description": "Moving subjects, lowercase singular nouns: person, dog, cat, car, truck, "
                           "package, bird, animal, bicycle. Empty if nothing moves.",
            "items": {"type": "string"},
        },
        "best_frame": {"type": "integer", "description": "1-based index of the frame that shows the action best."},
    },
    "required": ["title", "description", "subjects", "best_frame"],
    "additionalProperties": False,
}


# ---------- suspicious activity (opt-in per account; see docs/research/suspicious-activity.md) ----------

CONCERN_LEVELS = ("none", "suspicious", "threat")

# Judge actions, never people: the checklist names what counts and what never does, and a flag needs
# a named, visible action. Kept out of the prompt entirely while the setting is off.
CONCERN_PROMPT = """

Concern check. Set "concern" from what people visibly DO in this clip, never from who they are or \
how they look. Most clips are "none". Leave it "none" unless you can name the action.

"suspicious": only if one of these is clearly visible:
- trying door, window, gate or car handles without going in, or checking several in turn
- looking into windows or parked cars up close (face or light against the glass)
- picking up a package or item that was already on the porch, yard or driveway and carrying it off, \
unless the face matcher names a household member
- climbing a fence, wall or gate, or going in or out through a window
- covering, turning, unplugging or otherwise tampering with a camera or light
- staying in one spot near a door, window or vehicle on the property for most of the clip without an \
evident errand (waiting after knocking or ringing is an errand)
- moving along the house or between vehicles, trying or inspecting one entry point after another

"threat": something harmful is happening now: forcing a door or window, breaking glass, a weapon held \
or used, a fight or assault, setting a fire, smashing or spray-painting property.

Never a reason for concern on its own:
- race, skin colour, ethnicity, age, gender, body size, disability, or any other trait of a person
- clothing or accessories: hoodies, hats, masks or scarves, dark or work clothes, backpacks
- being a delivery driver, courier, mail carrier, contractor, gardener, utility worker, canvasser or \
salesperson; ringing the bell, knocking, leaving a package or flyer, taking a photo of it
- walking, running, cycling or driving past on the sidewalk or street, or turning around in the driveway
- being outside at night, in bad weather, or only briefly
- not being named by the face matcher (it only knows the people the homeowner enrolled)
- looking at the camera or doorbell; children playing; people walking dogs; animals of any kind
Do not describe mood or emotion, and do not guess at intent.

Time: use the local time given, not image brightness. Night can make a listed action more concerning, \
but night is never the reason by itself.
Earlier clips: you may get captions of earlier clips on this camera. Use them only to see whether a \
listed action is repeated (for example several approaches to doors). Never decide that two clips show \
the same person from how they look.
Normal activity: the homeowner may describe what is normal here; activity that matches it is "none".
Keep the title and description neutral and factual as usual; the concern goes only in "concern" and \
"concern_evidence"."""

CONCERN_FIELDS = {
    # Evidence comes before the verdict: structured outputs keep schema order, so the level is
    # written after the action is named. Not called "reasoning" (that invites a refusal).
    "concern_evidence": {
        "type": "string",
        "description": "If the concern is not 'none': the one visible action from the checklist that "
                       "justifies it, at most 20 words, describing the action, not a motive (e.g. "
                       "'tries the handle of the car door, then the next car'). Otherwise ''.",
    },
    "concern": {
        "type": "string",
        "enum": list(CONCERN_LEVELS),
        "description": "Default 'none'. 'suspicious' or 'threat' only per the concern checklist.",
    },
}

# A flag needs someone (or a vehicle) doing something; animals, weather and light never flag.
_ACTORS = {"person", "car", "truck", "van", "bus", "motorcycle", "bicycle", "vehicle"}


def output_schema(concern: bool) -> Dict[str, Any]:
    """The response schema; with `concern`, the two concern fields go between subjects and best_frame."""
    if not concern:
        return OUTPUT_SCHEMA
    schema = copy.deepcopy(OUTPUT_SCHEMA)
    props = {}
    for name, spec in OUTPUT_SCHEMA["properties"].items():
        if name == "best_frame":
            props.update(copy.deepcopy(CONCERN_FIELDS))
        props[name] = spec
    schema["properties"] = props
    schema["required"] = list(props)
    return schema


def settle_concern(level: Any, evidence: Any, subjects: List[str]) -> Tuple[str, str]:
    """Code-side guardrails on the model's verdict. Returns (level, evidence); ("none", "") unless
    the level is known, an action is named, and a person or vehicle is in the clip."""
    level = str(level or "").strip().lower()
    evidence = " ".join(str(evidence or "").split()).rstrip(".")
    if level not in CONCERN_LEVELS or level == "none" or not evidence:
        return "none", ""
    if not _ACTORS.intersection(subjects):
        return "none", ""
    return level, evidence[:200]


@dataclass
class ConcernContext:
    """What the concern check sees besides the frames. Only built when the account turned it on."""
    local_time: str = ""        # "Tue 21:47" in the owner's time zone; "" if unknown
    normal: str = ""            # owner's "what's normal here" note
    earlier: List[str] = field(default_factory=list)  # captions of recent clips on this camera

    def notes(self, duration: float, frames: int) -> List[str]:
        lines = []
        if self.local_time:
            lines.append(f"Local time: {self.local_time}")
        if duration > 0:
            lines.append(f"Clip length: {duration:.0f} s, {frames} frames evenly spaced")
        if self.normal:
            lines.append(f"Normal activity here (from the homeowner): {self.normal}")
        if self.earlier:
            lines.append("Earlier clips on this camera, last 30 min: " + "; ".join(self.earlier))
        return lines


@dataclass
class ClaudeVerdict:
    title: str = ""
    description: str = ""
    subjects: List[str] = field(default_factory=list)
    concern: str = "none"
    concern_evidence: str = ""
    concern_checked: bool = False  # the concern check ran on this clip (setting on, no refusal)
    snapshot: Optional[bytes] = None
    frames_analyzed: int = 0
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


class ClaudeUnavailable(Exception):
    """Temporary problem (rate limit, network, server). The clip should be retried later."""


class ClaudeAnalyzer:
    def __init__(self, api_key: Optional[str] = None) -> None:
        # Key from the dashboard if set; otherwise the SDK reads ANTHROPIC_API_KEY from the environment.
        kwargs = {"api_key": api_key} if api_key else {}
        self.client = anthropic.AsyncAnthropic(max_retries=3, timeout=60.0, **kwargs)
        self.api_key = api_key

    async def analyze_clip(self, video: bytes, camera: str, frames: int = 0, width: int = 0,
                           people: Optional[List[str]] = None,
                           concern: Optional[ConcernContext] = None) -> ClaudeVerdict:
        """Describe a clip. With `concern`, also check it for suspicious activity."""
        # Full-size frames for the notification snapshot; smaller copies for Claude keep token use low.
        snapshots, duration = await frames_and_duration(video, frames or config.CLAUDE_FRAMES)
        verdict = ClaudeVerdict(frames_analyzed=len(snapshots))
        if not snapshots:
            verdict.title = "Clip unreadable"
            verdict.description = "This clip could not be decoded."
            return verdict

        notes = [f"Camera: {camera}"]
        if people:
            notes.append("Face matcher note: recognized in this clip: " + ", ".join(people) + ".")
        images: List[Dict[str, Any]] = []
        for jpeg in snapshots:
            small = await downscale(jpeg, width or config.CLAUDE_FRAME_WIDTH)
            images.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg",
                           "data": base64.standard_b64encode(small).decode()},
            })

        response = None
        if concern is not None:
            extra = concern.notes(duration, len(snapshots))
            response = await self._request(notes + extra, images, check_concern=True)
            if response.stop_reason == "refusal":
                # Never lose the description over the concern check: describe the clip without it.
                log.warning("Claude declined the concern check for a clip from %s (%s); describing "
                            "it without the check", camera, response.stop_details)
                self._count(verdict, response)
                response = None
            else:
                verdict.concern_checked = True
        if response is None:
            response = await self._request(notes, images, check_concern=False)
        self._count(verdict, response)
        verdict.model = response.model

        if response.stop_reason == "refusal":
            log.warning("Claude declined to describe a clip from %s: %s", camera, response.stop_details)
            verdict.title = "Motion"
            verdict.description = "Claude declined to describe this clip."
            verdict.snapshot = snapshots[len(snapshots) // 2]
            return verdict
        if response.stop_reason == "max_tokens":
            raise ClaudeUnavailable("Response hit max_tokens before finishing")

        text = next((b.text for b in response.content if b.type == "text"), "")
        data = json.loads(text)
        verdict.title = data["title"].strip().rstrip(".")
        verdict.description = data["description"].strip()
        verdict.subjects = sorted({s.strip().lower() for s in data["subjects"] if s.strip()})
        if verdict.concern_checked:
            verdict.concern, verdict.concern_evidence = settle_concern(
                data.get("concern"), data.get("concern_evidence"), verdict.subjects)
        index = min(max(int(data["best_frame"]), 1), len(snapshots)) - 1
        verdict.snapshot = snapshots[index]
        return verdict

    @staticmethod
    def _count(verdict: ClaudeVerdict, response) -> None:
        verdict.input_tokens += response.usage.input_tokens
        verdict.output_tokens += response.usage.output_tokens

    async def _request(self, notes: List[str], images: List[Dict[str, Any]], check_concern: bool):
        content = [{"type": "text", "text": "\n".join(notes)}] + images
        try:
            return await self.client.beta.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=500 if check_concern else 400,
                system=SYSTEM_PROMPT + (CONCERN_PROMPT if check_concern else ""),
                messages=[{"role": "user", "content": content}],
                output_config={
                    "effort": config.CLAUDE_EFFORT,
                    "format": {"type": "json_schema", "schema": output_schema(check_concern)},
                },
                # If a safety classifier declines a frame set, retry on a fallback model server-side.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            raise ClaudeUnavailable(str(exc)) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500 or exc.status_code == 529:
                raise ClaudeUnavailable(f"{exc.status_code}: {exc.message}") from exc
            raise  # 400/401/403 etc.: a configuration problem, surfaced in the UI


async def check_api_key(api_key: str) -> None:
    """Raise if Anthropic rejects the key. Looks up the model, which costs no tokens."""
    client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=1, timeout=20.0)
    await client.models.retrieve(config.CLAUDE_MODEL)
