"""Clip descriptions with Claude: a few frames in, a short structured description out."""

import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import anthropic

from . import config
from .media import downscale, extract_frames

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


@dataclass
class ClaudeVerdict:
    title: str = ""
    description: str = ""
    subjects: List[str] = field(default_factory=list)
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
                           people: Optional[List[str]] = None) -> ClaudeVerdict:
        # Full-size frames for the notification snapshot; smaller copies for Claude keep token use low.
        snapshots = await extract_frames(video, frames or config.CLAUDE_FRAMES)
        verdict = ClaudeVerdict(frames_analyzed=len(snapshots))
        if not snapshots:
            verdict.title = "Clip unreadable"
            verdict.description = "This clip could not be decoded."
            return verdict

        content: List[Dict[str, Any]] = [{"type": "text", "text": f"Camera: {camera}"}]
        if people:
            content.append({"type": "text", "text": "Face matcher note: recognized in this clip: "
                            + ", ".join(people) + "."})
        for jpeg in snapshots:
            small = await downscale(jpeg, width or config.CLAUDE_FRAME_WIDTH)
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg",
                           "data": base64.standard_b64encode(small).decode()},
            })

        try:
            response = await self.client.beta.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=400,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}],
                output_config={
                    "effort": config.CLAUDE_EFFORT,
                    "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
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

        verdict.model = response.model
        verdict.input_tokens = response.usage.input_tokens
        verdict.output_tokens = response.usage.output_tokens

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
        index = min(max(int(data["best_frame"]), 1), len(snapshots)) - 1
        verdict.snapshot = snapshots[index]
        return verdict


async def check_api_key(api_key: str) -> None:
    """Raise if Anthropic rejects the key. Looks up the model, which costs no tokens."""
    client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=1, timeout=20.0)
    await client.models.retrieve(config.CLAUDE_MODEL)
