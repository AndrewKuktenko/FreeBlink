# Suspicious-activity labels for FreeBlink: prior art, bias, prompt design, and a plan

*Researched 2026-11-04. Sources are linked inline and collected at the end. Anything marked **UNVERIFIED** could not be confirmed against a primary source. Statements about what Anthropic's Usage Policy allows are a developer's reading, not a ruling from Anthropic.*

## Check on real clips (2026-11-04)

The implementation (§7, minus the optional "person at night" rule and learned baselines) was run on the owner's 52 cached clips: a Video Doorbell and a garage camera, 2–3 Nov 2026. Each clip went through the production code path: claude-sonnet-5-5, effort `low`, 4 frames at 640 px, with local time (America/New_York) and the last 30 minutes of captions on the same camera, from `events.json`. 20 clips had no stored metadata and ran without time or history.

| | Result |
|---|---|
| Clips flagged | **0 / 52** (target: 0, since none showed a listed action) |
| Refusals of the concern check | 0 |
| API errors (schema accepted) | 0 |
| Tokens per clip | 3,230 in / 89 out, against 2,023 / 76 without the check: about $0.0065 against $0.004 |

- **What the set covered:** kids running past and playing near the porch, people and a dog on the porch at 22:26–22:30, someone bending down on the porch at night, a hand at the front door, and people standing in the garage beside a car. These are the cases where a looser prompt would plausibly flag.
- **What it can't show:** this set has no real positives, so whether the check catches a listed action is untested. Watch the first real flags and the *Not suspicious* rate, as §7.6 suggests.

## TL;DR

**Add two fields to the existing single Claude call: `concern_evidence` (a short description of the observed action) and then `concern` (`none` | `suspicious` | `threat`, default `none`). Wire them in with a behaviour-only checklist, a short "earlier clips on this camera" note, and code-side guardrails. Make it opt-in in Settings.**

- **Prior art:** Frigate already does almost this. It has a `potential_threat_level` of 0/1/2, an editable `activity_context_prompt` that lists normal, suspicious and critical behaviours, and a schema that puts `observations` before the verdict ([source](https://github.com/blakeblackshear/frigate/blob/v0.18.2/frigate/genai/prompts.py)). Ring's Unusual Event Alert takes a different route. It learns what is normal for each camera and shows why it flagged an event ([Ring help](https://ring.com/support/articles/97l0i/Ring-AI-Video-Descriptions-Single-Event-Alert-Beta)).
- **Judge actions, never people.** Neighbors and Nextdoor showed that "suspicious" posts mostly flagged people of color ([VICE](https://www.vice.com/en/article/amazons-home-security-company-is-turning-everyone-into-cops/)). Nextdoor's fix was a form that made posters describe behaviour, which it says cut racial-profiling posts by 75% ([BuzzFeed News](https://www.buzzfeednews.com/article/carolineodonovan/nextdoor-rolls-out-product-fix-it-hopes-will-stem-racial-pro)). Vision-language models carry the same bias: CLIP put 16.5% of male face images into crime-related classes ([Radford et al.](https://arxiv.org/abs/2103.00020)). The prompt therefore lists which actions count, and spells out what never counts.
- **Claude-specific schema traps** ([structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)):
  - required properties come out in schema order, so the evidence field can come before the verdict;
  - a field named like "reasoning" can trigger a `reasoning_extraction` refusal, so ask for "a short explanation" or "evidence" instead;
  - enum casing isn't guaranteed, so use single lowercase words.
- **The notification needs headroom.** FreeBlink already sends **priority 5 (max) for every clip with a person in it** ([app/events.py](../../app/events.py)), so a suspicious clip can't sound more urgent today. Move ordinary person clips to 4 and keep 5 for flagged clips. On Android, the owner can then let only priority 5 override Do Not Disturb ([ntfy docs](https://docs.ntfy.sh/subscribe/phone/)).
- **Arm state tells us nothing.** Blink doesn't record motion clips while disarmed ([Blink support](https://support.blinkforhome.com/en_US/motion-detection/understanding-motion-detection)), so nearly every clip comes from an armed system. The cheap signals that do help are local time, camera name, the face-matcher names, clip length, and the last ~30 minutes of captions on the same camera.

---

## 1. How this fits the current code

- **One call per clip.** `ClaudeAnalyzer.analyze_clip` ([app/claude_vision.py](../../app/claude_vision.py)) sends:
  - the camera name;
  - the face-matcher note, if any;
  - 4 JPEGs at 640 px, taken at evenly spaced points ((i+0.5)/4 of the clip, [app/media.py](../../app/media.py)).

  The call uses `effort: low` and a strict JSON schema (`title`, `description`, `subjects`, `best_frame`). A refusal becomes a generic "Motion" caption.
- **The prompt already bans the risky part.** It says "No speculation about identity or intent. Never guess who someone is from how they look." The new label has to keep that rule. It describes an *action* ("tries the car door handle") and doesn't name a motive ("tries to break into the car").
- **Event and push.** `_process_claude` ([app/events.py](../../app/events.py)) stores the event dict in `events.json`, then pushes:
  - `priority=5 if "person" in verdict.subjects else 4`;
  - tags from the `EMOJI` map.

  `/api/clips` ([app/main.py](../../app/main.py)) copies `title`, `description` and `subjects` into `clip.ai`. `renderClip` and `openClip` ([static/app.js](../../static/app.js)) show them.
- **History is already on hand.** `check_once` handles new clips one by one, oldest first, and stores each event before the next clip. `self.store.events` (newest first, up to `EVENTS_KEEP`) can therefore supply the previous clips on the same camera at no cost. `_split_stationary` already does this for the YOLO engine.
- **Settings pattern.** `UserSettings.faces_enabled` / `set_faces_enabled` ([app/settings.py](../../app/settings.py)) is the model for an opt-in toggle.

## 2. Prior art

### 2.1 Frigate GenAI review summaries (the closest match)

Frigate v0.18.2 (2026-10-15) sends the frames of a finished review item to a vision LLM and stores structured metadata ([docs](https://docs.frigate.video/configuration/genai/review_summaries)).

**Schema.** `ReviewMetadata` ([types.py](https://github.com/blakeblackshear/frigate/blob/v0.18.2/frigate/data_processing/post/types.py)) has these fields, in this order:

| Field | Type and limits |
|---|---|
| `observations` | 3–8 strings, each 20–200 chars, "in chronological order" |
| `scene` | 150–600 chars |
| `title` | |
| `shortSummary` | |
| `confidence` | 0–1 |
| `potential_threat_level` | integer 0–2: "0 = normal, 1 = suspicious, 2 = critical threat" |
| `other_concerns` | optional list |

The verdict comes last, after the model has listed what it saw. The schema is sent with `"strict": True` ([prompts.py](https://github.com/blakeblackshear/frigate/blob/v0.18.2/frigate/genai/prompts.py)).

**Prompt** (`build_review_description_prompt`, same file):
- It tells the model to "Describe the scene based on observable actions and movements, evaluate the activity against the Activity Indicators above, and assign a potential_threat_level".
- Time of day comes from the timestamp, not the picture: "do not infer time from image brightness or darkness. Unusual hours … should increase suspicion when the observable behavior itself appears questionable."
- It warns: "Unidentified objects (without names) are NOT indicators of suspicious activity".
- It passes the camera, frame count, start time, duration, zones, and tracked objects. Recognised faces are written as `Name ← person`.

**Config** ([review.py](https://github.com/blakeblackshear/frigate/blob/v0.18.2/frigate/config/camera/review.py)):
- `review.genai.enabled` is off by default.
- `additional_concerns` takes user strings such as "animals in the garden".
- `activity_context_prompt` can be set globally or per camera. Its default lists:
  - **Level 0:** "Known/verified people in any zone at any time"; deliveries and services 6 AM–10 PM; "Activity confined to public areas only (sidewalks, streets)".
  - **Level 1:** "trying handles without entering, peering through windows, examining multiple vehicles"; "Taking items that don't belong to them"; climbing fences; "Attempting to conceal actions or items"; "Prolonged loitering".
  - **Level 2:** weapons, break-in tools, forced entry, violence, active damage or theft.
- One default rule doesn't fit FreeBlink. It makes "Unidentified person in private areas … during late night/early morning (11 PM - 5 AM)" "ALWAYS Level 1 regardless of activity or duration". That turns presence plus time into suspicion; §7.2 doesn't follow it.
- The docs caution that the context "is not overly specific as it can sway the output".

**Downstream use** ([review_descriptions.py](https://github.com/blakeblackshear/frigate/blob/v0.18.2/frigate/data_processing/post/review_descriptions.py)):
- Reports only include items where `potential_threat_level > 0` or a concern matched.
- Native notifications show `title` and `shortSummary` ([docs](https://docs.frigate.video/configuration/genai/review_summaries)).

### 2.2 Home Assistant LLM Vision

The `event_summary` blueprint in v1.7.2 ([blueprint](https://github.com/valentinfrlch/ha-llmvision/blob/v1.7.2/blueprints/event_summary.yaml)) has an "Important (Experimental)" option. It makes a *second* LLM call with this prompt: "Choose from the following options: 'passive' for unimportant events, 'time-sensitive' for notable but non-critical events such as a person at the front door, and 'critical' only for potential burglaries or highly suspicious activity."

- The answer is used directly as the iOS `interruption-level`.
- `passive` events aren't sent at all.
- The option text warns: "Critical events override 'Do Not Disturb' settings. Use with caution: AI can make mistakes."

### 2.3 Commercial vendors

| Vendor | What gets flagged, and on what basis | Source |
|---|---|---|
| **Ring** | **Unusual Event Alert (beta)**:<br>• "analyzes motion detection patterns from your recent history and the type of activity", so "unusual" is learned per camera, with a learning period;<br>• a short delay "as the system checks for similar events";<br>• the owner can "read why the system marked the event as unusual";<br>• not available in Illinois.<br>**Active Warnings** speak a deterrent "based on details like location and actions" and are set per mode (Away/Home/Disarmed). The help page says Video Descriptions "might occasionally be inaccurate". | [Ring help](https://ring.com/support/articles/97l0i/Ring-AI-Video-Descriptions-Single-Event-Alert-Beta), [CES 2026 post](https://blog.ring.com/?p=7755) |
| **Google Nest (Gemini for Home)** | AI descriptions and notifications, Home Brief, "Unfamiliar visitor" search. The help page has no suspicious or unusual flag. A Home Premium infosheet reportedly mentions unusual-activity alerts (**UNVERIFIED**). | [Google help](https://support.google.com/googlenest/answer/15542305) |
| **Wyze** | Descriptive Alerts / Wyze Stories: no suspicious flag in the terms. The terms say the feature "does not use the images to identify or try to identify any particular person" and doesn't "always make correct inferences". | [Wyze terms](https://www.wyze.com/pages/terms-for-descriptive-alerts) |
| **Arlo** | Event Captions in Secure 6. No first-party documentation of an unusual-activity flag was found (**UNVERIFIED**). | [Arlo press release](https://www.nasdaq.com/press-release/arlo-announces-secure-6-smart-home-security-service-2025-05-08) |
| **UniFi Protect** | Rule-based "loitering zones" (dwell time in a zone), plus "NeXT AI" summaries on the AI Key. Only third-party mirrors of the release notes were found (**UNVERIFIED**). | [release-notes mirror](https://redlib.hyperreal.coffee/r/unifi_versions/comments/1jrm7qt/unifi_protect_application_5338) |
| **Eufy** | Loitering detection on some models; no official page found (**UNVERIFIED**). | [community thread](https://community.eufy.com/t/loitering-pop-up/4235120) |
| **Scrypted NVR** | AI Summaries (title and summary pushed with thumbnails). No threat or priority field. | [Scrypted docs](https://docs.scrypted.app/scrypted-nvr/ai-summaries.html) |

**Pattern.** Two approaches show up:
- **Rule lists:** a behaviour list judged by an LLM (Frigate, LLM Vision) or geometry rules (loitering zones).
- **Learned baselines:** what is unusual for *this* camera (Ring).

Only Ring and Frigate explain the flag. Each vendor that ships something also warns that it can be wrong.

## 3. Behaviour, not appearance

### 3.1 The documented profiling problem

- **Ring Neighbors.** "Motherboard individually reviewed more than 100 user-submitted posts in the Neighbors app between December 6 and February 5" and found that most people reported as suspicious were people of color ([VICE, 2019-02-07](https://www.vice.com/en/article/amazons-home-security-company-is-turning-everyone-into-cops/)). NBC reported in 2025 that privacy advocates say Ring's police partnerships and Neighbors "heightened the risk of racial profiling" ([NBC News](https://www.nbcnews.com/business/business-news/amazons-ring-launches-ai-generated-security-alerts-rcna214970)).
- **Nextdoor, 2016.** The Crime & Safety form was rebuilt so that:
  - posts lead with the behaviour;
  - a post that mentions race needs two other descriptors, such as "Nike sneakers, blue jeans" ([NPR/MPR, 2016-08-23](https://www.mprnews.org/story/2016/08/23/npr-nextdoor-block-racial-profiling));
  - posters see the prompt "Is what I saw actually suspicious, if I take race or ethnicity out of the question?"

  Nextdoor says the forms "reduced incidents of racial profiling on its network by 75% during tests". It also reported that abandoned reports rose by 50% ([BuzzFeed News, 2016-08-24](https://www.buzzfeednews.com/article/carolineodonovan/nextdoor-rolls-out-product-fix-it-hopes-will-stem-racial-pro)). An earlier pilot reported "roughly a 50 percent reduction" (NPR). Both figures are Nextdoor's own numbers, with no independent check. The original Nextdoor blog posts now return 404.
- **Lesson for FreeBlink.** Write the suspicious list as actions, and spell out what never counts. Friction is a feature: if the model can't name an action, it can't flag.

### 3.2 Models have the same bias

- **CLIP.** On FairFace, "16.5% of male images were misclassified into classes related to crime". The rate for people aged 0–20 was about 18%, with "significant disparities in classifications across races for crime related terms". Adding a "child" class "drastically reduced" the crime-related labels for people under 20 ([Radford et al. 2021, §7.1](https://arxiv.org/abs/2103.00020)). The lesson: the options you give the model shape whom it picks.
- **Robots.** A CLIP-powered robot asked to "pack the criminal block" picked faces along racial lines ([Hundt et al., FAccT 2022](https://arxiv.org/abs/2207.11569)).
- **Implication.** Never ask Claude whether a *person* looks suspicious. Ask whether a listed *action* is visible, and require the action to be named.

### 3.3 Anthropic policy and docs (quoted)

From the Usage Policy ([AUP](https://www.anthropic.com/legal/aup); the page read on 2026-11-04 shows "Effective Nov 12, 2026"):

- Under "Do Not Use for Surveillance, Censorship, or Prohibited Law Enforcement or Criminal Justice Purposes":
  - "Make or suggest decisions in law enforcement or criminal justice processes, including decisions to investigate, charge, arrest or detain…"
  - "Target or track a person or their property without that person's consent by monitoring their physical location, biometrics, movements … whether in real time, on an ongoing basis, or through analysis of previously collected data. This includes using our products for facial recognition, … predictive policing…"
  - "Utilize models to assign scores or ratings to individuals based on an assessment of their trustworthiness or social behavior without notification or their consent"
  - "Build or support emotional recognition systems or techniques that are used to infer emotions of a natural person, except for medical or safety reasons"
  - "Utilize models as part of any biometric categorization system for categorizing people based on their biometric data to infer their race…"
- Under hateful behaviour: "Engage in or promote discriminatory practices or behaviors against individuals or groups on the basis of one or more protected attributes, including race, ethnicity…"
- Under High-risk Physical Actions, requiring human control: "Control safety systems — arm, disarm, trigger, or override … alarms, … or locks".

From the vision docs ([vision](https://platform.claude.com/docs/en/build-with-claude/vision)):
- "Claude cannot be used to name people in images and refuses to do so".
- "Do not use Claude for tasks requiring perfect precision or sensitive image analysis without human oversight."

**Reading for FreeBlink (interpretation, not confirmed by Anthropic).** Flagging an *event* on your own property for your own review is a description task. The design should stay clear of anything that reads as scoring or tracking *people*:

- the label belongs to the clip, not to a person;
- no "suspect" profiles;
- no linking unknown people across clips by appearance (the same line as the face-recognition doc, [§6.6](face-recognition.md#66-unknown-person-alerts));
- no emotion words ("nervous", "agitated") in the criteria;
- no automatic arming, siren or police reporting from the label;
- the owner always sees the evidence and can clear the flag.

## 4. Model and prompt design

### 4.1 What the research says

- **Zero-shot VLMs over-flag; rules help.** SmartHome-Bench ([Zhao et al. 2025](https://arxiv.org/abs/2506.12992)) has 1,203 smart-home clips, balanced between normal and abnormal, with security as the largest category. Results for Claude-3.5-Sonnet:

  | Method | Accuracy | Precision | Recall |
  |---|---|---|---|
  | Zero-shot | 70.82 | 69.66 | 81.36 |
  | Chain of thought (CoT) | 71.90 | 83.44 | 59.78 |
  | TRLC (rules from a taxonomy, then predict, then self-reflect) | 79.05 | 79.67 | 82.13 |

  - Zero-shot recall is high and precision is lower, so it over-flags.
  - CoT raised precision and cut recall.
  - Few-shot "does not fundamentally enhance CoT performance".
  - My inference: real homes have far fewer anomalies than a balanced benchmark, so a zero-shot flag would mostly be false.
- **Rules from normal examples.** AnomalyRuler ([Yang et al., ECCV 2024](https://arxiv.org/abs/2407.10299)) builds rules from a few *normal* reference samples, then applies them. It adds "perception smoothing" and "robust reasoning" to cut false alarms. This is the research version of Frigate's `activity_context_prompt` and Ring's learned baseline. A short owner-written "what's normal here" note is the cheap equivalent.
- **Don't trust self-reported confidence.** "LLMs, when verbalizing their confidence, tend to be overconfident" ([Xiong et al., ICLR 2024](https://arxiv.org/abs/2306.13063)). Frigate's `confidence` field adds tokens without adding a usable signal. Require a named action instead.
- **Few frames.** Four evenly spaced frames from a 10–30 s clip can't show "lingering" or "trying several handles" reliably. Frigate sends up to 20 frames plus the duration ([docs](https://docs.frigate.video/configuration/genai/review_summaries)). FreeBlink should pass the clip length, and treat repetition *across clips* as the loitering signal (§5).

### 4.2 Schema mechanics on Claude (structured outputs)

From [platform.claude.com structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs):

- **Ordering.** "properties in objects maintain their defined ordering from your schema, with one important caveat: required properties appear first". Mark every field required and put the evidence before the verdict, so the label is written after the description and the evidence.
- **Don't ask for "reasoning".** "A property that asks for the model's thinking or step-by-step reasoning might lead to a `reasoning_extraction` refusal. Ask for a short explanation instead." The refusals page lists "A `reasoning`, `thinking`, or `trace` field in JSON output" as a trigger ([refusals](https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback#keep-reasoning-in-thinking-blocks)). Name the field `concern_evidence` and ask for "the visible action".
- **Enums.** "Structured outputs don't guarantee the capitalization of string `enum` … values". Use single lowercase words and compare case-insensitively.
- **Length limits aren't enforced.** `minLength`/`maxLength` aren't supported, so put length limits in the description text, as FreeBlink already does.
- **Refusals.** A refusal "might not match your schema". The existing refusal branch must leave `concern="none"`.

## 5. Context signals beyond the frames

| Signal | Value | Cost | Notes |
|---|---|---|---|
| **Local time** ("21:47, after dark") | High: it makes listed actions weightier | ~10 tokens | Frigate passes the timestamp and tells the model not to judge time from brightness. Blink `created_at` is UTC, so FreeBlink needs a time zone: a `TZ` env var or the browser's `Intl` zone saved in settings. Whether Blink's network info carries a `time_zone` field is **UNVERIFIED**. |
| **Camera name** | Medium: tells front door from backyard | already sent | |
| **Face-matcher names** | High for *lowering* concern | already sent | Frigate's default makes known people Level 0. A matched name means "household member", not "only household members present". |
| **Clip length** | Medium | ~5 tokens | `extract_frames` already runs ffprobe for the duration; return it. |
| **Earlier clips on this camera** (last 30 min: time, title, concern) | High for loitering or casing | ~50–80 tokens | Ring's alert relies on "recent history"; Frigate's report adds overlapping events from other cameras. Keep it at clip level (see §7.4). |
| **Owner's "what's normal here" note** | Medium | ~50–100 tokens | Frigate's `activity_context_prompt` and AnomalyRuler's normal-rule induction, in one short text box. |
| **Arm state** | ≈ none | | Blink: "your cameras will not record motion events" when disarmed ([Blink support](https://support.blinkforhome.com/en_US/motion-detection/understanding-motion-detection)), so clips almost always come from an armed system. blinkpy's `sync.arm` only reads `network.armed` ([sync_module.py](https://github.com/fronzbot/blinkpy/blob/dev/blinkpy/sync_module.py)). Ring's per-mode setting (Away/Home) has no Blink equivalent. |

## 6. Notification UX

**ntfy priority behaviour** ([publish docs](https://docs.ntfy.sh/publish/#message-priority), [phone docs](https://docs.ntfy.sh/subscribe/phone/)):

| Priority | Name | Behaviour |
|---|---|---|
| 5 | max/urgent | "Really long vibration bursts, default notification sound with a pop-over notification" |
| 4 | high | "Long vibration burst, … pop-over" |
| 3 | default | "Short default vibration and sound" |
| 2 | low | silent, hidden until the drawer is pulled down |
| 1 | min | silent, under "Other notifications" |

**Android** has "one notification channel for each priority", and each channel can "override the Do Not Disturb (DND)" setting. That lets an owner have only the priority-5 channel break through DND.

**iOS.** The ntfy-ios source maps:
- 4 → `.timeSensitive`;
- 5 → `.critical`, but only when the user turned on critical alerts and iOS granted permission (otherwise `.timeSensitive`);
- 1–2 → `.passive` ([NotificationContent.swift](https://github.com/binwiederhier/ntfy-ios/blob/main/ntfy/Utils/NotificationContent.swift)).

The critical-alerts code landed in June 2026. Whether it is in the App Store build is **UNVERIFIED**.

**Tags.** Tags that match emoji shortcodes are "converted to an emoji and prepended to title or message"; other tags show as text ([publish docs](https://docs.ntfy.sh/publish/#tags-emojis)). `warning` (⚠️) and `rotating_light` (🚨) are the natural choices.

**Crying wolf is well documented:**
- 94–98% of burglar-alarm calls to police are false ([Sampson, *False Burglar Alarms*, COPS/POP guide, 2002](https://www.ojp.gov/index%2Ephp/node/1052616));
- in hospitals, "between 85 and 99 percent of alarm signals do not require clinical intervention", and "clinicians become desensitized … 'alarm fatigue'", then "turn down the volume … turn it off" ([Joint Commission Sentinel Event Alert 50](https://digitalassets.jointcommission.org/api/public/content/f65e5c9df2b94000a99445e0a7877007));
- Ring sells Unusual Event Alert as a way to cut notification fatigue (help page above).

**The FreeBlink problem.** Every person clip is already priority 5. A flagged clip only stands out if ordinary clips get quieter.

## 7. Recommendation for FreeBlink

### 7.1 Schema additions (only when the feature is on)

Build `OUTPUT_SCHEMA` dynamically, the way Frigate drops `other_concerns`. When the setting is off, the schema and prompt stay exactly as they are today. Insert the two fields after `subjects` and before `best_frame`, both required:

```python
"concern_evidence": {
    "type": "string",
    "description": "If the concern is not 'none': the one visible action from the checklist that "
                   "justifies it, at most 20 words, describing the action, not a motive "
                   "(e.g. 'tries the handle of the car door, then the next car'). Otherwise ''.",
},
"concern": {
    "type": "string",
    "enum": ["none", "suspicious", "threat"],
    "description": "Default 'none'. 'suspicious' or 'threat' only per the checklist.",
},
```

- A boolean would mix "worth a look" with "happening now". Three levels match Frigate's 0/1/2 and map cleanly to the UI and push.
- Don't add a `confidence` field (§4.1).

### 7.2 Prompt text (append to `SYSTEM_PROMPT` when enabled)

```text
Concern check. Set "concern" from what people visibly DO in this clip, never from who they are or
how they look. Most clips are "none". Leave it "none" unless you can name the action.

"suspicious" — only if one of these is clearly visible:
- trying door, window, gate or car handles without going in, or checking several in turn
- looking into windows or parked cars up close (face or light against the glass)
- picking up a package or item that was already on the porch, yard or driveway and carrying it off,
  unless the face matcher names a household member
- climbing a fence, wall or gate, or going in or out through a window
- covering, turning, unplugging or otherwise tampering with a camera or light
- staying in one spot near a door, window or vehicle on the property for most of the clip
  without an evident errand (waiting after knocking or ringing is an errand)
- moving along the house or between vehicles, trying or inspecting one entry point after another

"threat" — something harmful is happening now: forcing a door or window, breaking glass, a weapon
held or used, a fight or assault, setting a fire, smashing or spray-painting property.

Never a reason for concern on its own:
- race, skin colour, ethnicity, age, gender, body size, disability, or any other trait of a person
- clothing or accessories: hoodies, hats, masks or scarves, dark or work clothes, backpacks
- being a delivery driver, courier, mail carrier, contractor, gardener, utility worker, canvasser
  or salesperson; ringing the bell, knocking, leaving a package or flyer, taking a photo of it
- walking, running, cycling or driving past on the sidewalk or street, or turning around in the driveway
- being outside at night, in bad weather, or only briefly
- not being named by the face matcher (it only knows the people the homeowner enrolled)
- looking at the camera or doorbell; children playing; people walking dogs; animals of any kind
Do not describe mood or emotion ("nervous", "shifty"), and do not guess at intent.

Time: use the local time given, not image brightness. Night can make a listed action more
concerning, but night is never the reason by itself.
Earlier clips: you may get captions of earlier clips on this camera. Use them only to see whether a
listed action is repeated (for example several approaches to doors). Never decide that two clips show
the same person from how they look.
Keep the title and description neutral and factual as usual; the concern goes only in "concern"
and "concern_evidence".
```

This intentionally doesn't use Frigate's "unknown person at night is always suspicious" rule. If the owner wants it, offer it as a separate, deterministic rule (§7.7).

### 7.3 Context to pass (extra text blocks before the images)

```text
Camera: Front door
Local time: Tue 21:47 (after dark)
Clip length: 14 s; frames at about 2, 5, 9, 12 s
Face matcher note: recognized in this clip: Anna.            # existing, when present
Normal activity here (from the homeowner): Gardener Tuesdays 8-12. Neighbour's kids cut across the lawn.
Earlier clips on this camera, last 30 min: 21:31 "Person walks up the driveway" (none);
21:39 "Person at the side gate" (suspicious: tries the gate latch).
```

- **Local time:** pass the "after dark" flag only if it is cheap to compute (a sunrise/sunset table from a configured lat/long). Otherwise pass the time alone.
- **Owner note:** cap it at about 300 characters, as Frigate warns that overly specific context sways results.

### 7.4 History across clips

- **Selection.** In `_process_claude`, take `self.store.events` where:
  - `camera` matches;
  - `created_at` is within 30 minutes;
  - at most 5 entries.

  Pass each entry's time, `title`, `concern` and evidence.
- **Clip-level, never person-level.** Pass captions, not images or face data. Don't build "same stranger" clusters. This keeps to the AUP's "track a person … through analysis of previously collected data" and to face-recognition doc §6.6.
- **Repetition is code-side.** Count the clips with `person` in `subjects` and no recognised names on this camera in the last 15 minutes. If there are 3 or more, the model gets the earlier captions above; code doesn't escalate on the count alone. The prompt allows `suspicious` only when a listed action shows up across them.

### 7.5 Event, UI and notification

- **Event dict.** Add `"concern"` and `"concern_evidence"` to the event dict. `/api/clips` copies both into `clip.ai`.
- **`renderClip`.** Add a small pill next to `.clip-title`:
  - amber "Suspicious" with the `alert` icon;
  - red "Alert" for `threat`.

  Add it to the row's `aria-label` (", flagged suspicious"). Nothing is shown for `none`.
- **`openClip`.** Under the description, add one line: "Flagged: tries the front door handle twice." Add a **Not suspicious** button that posts the change and sets `concern_cleared: true` on the event. The cleared state is the owner's feedback signal; log it to tune the prompt.
- **Push.**

  | Concern | Title prefix | Tag | Priority |
  |---|---|---|---|
  | `suspicious` | "Suspicious: " | `warning` | 5 |
  | `threat` | "Alert: " | `rotating_light` | 5 |
  | `none`, with a person | | | 4 (today 5) |
  | `none`, other subjects | | | 3 (today 4) |

  This needs the owner's sign-off because it changes current behaviour. Settings should explain that only priority 5 should be allowed to override DND (Android channel, iOS critical alerts).

### 7.6 False-positive guardrails (code, not prompt)

1. **No evidence, no flag.** If `concern != "none"` and `concern_evidence` is empty, store `none`.
2. **Needs a person or vehicle.** If `subjects` contains neither `person` nor a vehicle, store `none`. Animals and weather never flag.
3. **Refusal or fallback.** On a refusal, an unreadable clip or a `max_tokens` stop, store `none`.
4. **Rate limit.** At most one priority-5 push per camera every 10 minutes. Later flagged clips still get the label, at priority 4.
5. **Case-insensitive enum.** Compare in lowercase, and treat unknown values as `none`.
6. **No side effects.** Never arm, disarm, trigger anything, or share with neighbours or police from the label (AUP High-risk Physical Actions).
7. **Measure before launch.** Before turning it on for anyone, run it over the 52 clips from the face spike plus a few staged clips (the owner trying their own car door at night, a courier at dusk). Target zero flags on ordinary clips. Afterwards, if more than about 1 in 50 clips is flagged, or "Not suspicious" is pressed often, tighten the checklist.

### 7.7 Settings

- **Toggle.** "Flag suspicious activity" is off by default. It is stored as `suspicious_enabled` in `UserSettings`, following the `faces_enabled` pattern. The help text: "Claude labels clips where someone does something from a short list (tries door handles, looks into cars, takes a package, climbs a fence…). It never judges people by how they look. It can be wrong, so check the clip."
- **Optional "What's normal here" text box.** About 300 characters.
- **Optional deterministic rule, separate from the model.** "Notify when a person is seen at night." It triggers on `person` in subjects, no recognised names, and the local time inside a user-set window. It uses its own neutral label ("Person at night"), not "Suspicious". This keeps owner-chosen presence rules apart from behaviour judgments.
- **Cost.** About 450 extra system-prompt tokens, 50–150 context tokens and about 30 output tokens per clip. With `effort: low` and 4 images, the increase is small (estimate, not measured).

### 7.8 Open questions

- Should a per-camera "learned normal" (Ring-style) come later? A cheap version: pass "this camera usually sees: deliveries 9–18, cars at 8 and 18" summarised from 2 weeks of events. Check it against the AUP first.
- Is the iOS critical-alert path in the shipped ntfy app, and does it need an entitlement on self-hosted servers? (**UNVERIFIED**)
- Should `threat` skip the 10-minute rate limit?

## Sources

**Frigate**
- Review summaries docs: https://docs.frigate.video/configuration/genai/review_summaries
- Prompt and schema: https://github.com/blakeblackshear/frigate/blob/v0.18.2/frigate/genai/prompts.py
- `ReviewMetadata`: https://github.com/blakeblackshear/frigate/blob/v0.18.2/frigate/data_processing/post/types.py
- Config defaults: https://github.com/blakeblackshear/frigate/blob/v0.18.2/frigate/config/camera/review.py
- Report filter: https://github.com/blakeblackshear/frigate/blob/v0.18.2/frigate/data_processing/post/review_descriptions.py

**Home Assistant / Scrypted**
- LLM Vision blueprint: https://github.com/valentinfrlch/ha-llmvision/blob/v1.7.2/blueprints/event_summary.yaml
- Scrypted AI Summaries: https://docs.scrypted.app/scrypted-nvr/ai-summaries.html

**Vendors**
- Ring Unusual Event Alert: https://ring.com/support/articles/97l0i/Ring-AI-Video-Descriptions-Single-Event-Alert-Beta
- Ring CES 2026 post: https://blog.ring.com/?p=7755
- Google Gemini for Home camera features: https://support.google.com/googlenest/answer/15542305
- Wyze Descriptive Alerts terms: https://www.wyze.com/pages/terms-for-descriptive-alerts
- Arlo Secure 6 press release: https://www.nasdaq.com/press-release/arlo-announces-secure-6-smart-home-security-service-2025-05-08
- UniFi release-notes mirror: https://redlib.hyperreal.coffee/r/unifi_versions/comments/1jrm7qt/unifi_protect_application_5338
- Eufy community thread: https://community.eufy.com/t/loitering-pop-up/4235120
- Blink motion detection: https://support.blinkforhome.com/en_US/motion-detection/understanding-motion-detection
- blinkpy `sync_module.py`: https://github.com/fronzbot/blinkpy/blob/dev/blinkpy/sync_module.py

**Profiling and bias**
- VICE on Ring Neighbors: https://www.vice.com/en/article/amazons-home-security-company-is-turning-everyone-into-cops/
- NBC News on Ring AI alerts: https://www.nbcnews.com/business/business-news/amazons-ring-launches-ai-generated-security-alerts-rcna214970
- NPR/MPR on Nextdoor: https://www.mprnews.org/story/2016/08/23/npr-nextdoor-block-racial-profiling
- BuzzFeed News on Nextdoor: https://www.buzzfeednews.com/article/carolineodonovan/nextdoor-rolls-out-product-fix-it-hopes-will-stem-racial-pro
- CLIP (Radford et al. 2021): https://arxiv.org/abs/2103.00020
- Robots Enact Malignant Stereotypes (Hundt et al. 2022): https://arxiv.org/abs/2207.11569

**Anthropic**
- Usage Policy: https://www.anthropic.com/legal/aup
- Vision: https://platform.claude.com/docs/en/build-with-claude/vision
- Structured outputs: https://platform.claude.com/docs/en/build-with-claude/structured-outputs
- Refusals: https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback

**Research**
- SmartHome-Bench: https://arxiv.org/abs/2506.12992
- AnomalyRuler: https://arxiv.org/abs/2407.10299
- Xiong et al., verbalized confidence: https://arxiv.org/abs/2306.13063

**Notifications and alert fatigue**
- ntfy publish docs: https://docs.ntfy.sh/publish/
- ntfy phone docs: https://docs.ntfy.sh/subscribe/phone/
- ntfy-ios `NotificationContent.swift`: https://github.com/binwiederhier/ntfy-ios/blob/main/ntfy/Utils/NotificationContent.swift
- Sampson, *False Burglar Alarms*: https://www.ojp.gov/index%2Ephp/node/1052616
- Joint Commission Sentinel Event Alert 50: https://digitalassets.jointcommission.org/api/public/content/f65e5c9df2b94000a99445e0a7877007
