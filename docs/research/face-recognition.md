# Face recognition for FreeBlink: options, licensing, law, and a plan

*Researched 2026-11-04. Sources are linked inline and collected at the end. Anything marked **UNVERIFIED** could not be confirmed against a primary source. The legal section is a developer's summary, **not legal advice**.*

## Decision (2026-11-04)

**We use InsightFace (SCRFD detector + ArcFace recogniser, `buffalo_l`; `buffalo_s` on small servers) on onnxruntime.** The owner's reasoning: FreeBlink is a free, non-commercial project, and InsightFace is clearly more accurate on small and angled faces (§3.2). The research below is kept as written. OpenCV YuNet + SFace (§3.1) stays the fallback if the licence position changes or if someone wants a permissive build.

What this decision does and doesn't settle:

- **Licence.** InsightFace's pretrained models are offered "for non-commercial research purposes only". FreeBlink meets the "non-commercial" part. Whether a free public app counts as "research" is a grey area. Two habits reduce the risk:
  - don't bake the weights into the public image; download them on first use from InsightFace's own release URL, with a hash check;
  - revisit this if FreeBlink ever takes money (donations, a paid tier).
- **Biometric law (§5) is a separate question.** BIPA and the GDPR apply whether or not the app is commercial. BIPA covers individuals, and the GDPR household exemption doesn't cover the operator of a hosted service. Self-host first stays the plan.
- **Integration.** Load the ONNX files directly with `onnxruntime` and do the 5-point alignment ourselves. The `insightface` pip package pulls in GUI OpenCV, scipy and scikit-image (about 455–521 MB installed). Everything else in §6 (engine interface, gallery, pipeline, enrollment, storage) carries over unchanged.

## Spike results (2026-11-04)

**Setup**
- 52 real clips from the owner's account (1280×720, about 10 s each).
- SCRFD + ArcFace ONNX files loaded directly with onnxruntime 1.31.1; no `insightface` package.
- Memory was measured in a `python:3.12-slim` Linux container limited to 1 GB RAM, no swap and 1 CPU (Apple M5 host). The script is in the session scratchpad and isn't committed.
- **Production server:** `VM.Standard.E2.1.Micro` in us-ashburn-1. It's x86 AMD with 1 GB RAM, and the console showed about 36% memory in use with FreeBlink running.

| | buffalo_s | buffalo_l |
|---|---|---|
| Model files needed (det + rec) | 16 MB | 191 MB |
| Process memory: idle / models loaded / peak at 1 fps / peak at 3 fps | 65 / 98 / 205 / 349 MB | 65 / 268 / 416 / 521 MB |
| Time per clip, 1 CPU, 10 frames (1 fps) | 0.26 s | 2.7 s |
| Detection / embedding per face | 22 ms / 14 ms | 265 ms / 124 ms |
| Clips with a face (3 fps) | 29 / 52 | 33 / 52 |
| Faces in IR frames (20% of frames were IR) | 7 | 14 |
| Median face width | 45 px | 42 px |

**Findings**
- Both models put the same person (a clear, frontal face) into one group across two clips.
- buffalo_s splits other people into more small groups. That failure mode is safe: they come out as "a person", not a wrong name.
- Both produced false or unusable faces: a car wheel (s), the back of a head (l), and silhouettes backlit by a window. The quality gates in §6.3 are required.
- Unrelated faces stayed below 0.40 cosine similarity, and the same face scored about 0.50 median. A starting match threshold of about 0.45 looks right; calibrate it after enrollment.
- **Use buffalo_s on the E2.1.Micro.** buffalo_l would take about 350 MB of the roughly 600 MB of free memory, and its CPU cost is about 10× higher.
- Real speed on the Micro's AMD core is not measured. It is UNVERIFIED and expected to be several times slower than these numbers.

## TL;DR (original research)

**Primary pick: OpenCV's YuNet face detector with the SFace recognizer, running locally on the CPU.** They run through `cv2.FaceDetectorYN` and `cv2.FaceRecognizerSF` from `opencv-python-headless`. Ship it as an optional module that is off by default, wired in the same way as the YOLO detector.

- **License:** this is the only candidate whose code *and* weights are permissively licensed: OpenCV is Apache-2.0, the YuNet weights are MIT and the SFace weights are Apache-2.0 ([YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet), [SFace](https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface)).
- **Size:** about 39 MB of models in total.
- **Install:** abi3 wheels exist for both arm64 and amd64, and no compiler is needed.
- **Accuracy:** good enough for a family-sized gallery (SFace LFW 99.40–99.60%). It is weaker on profile faces, which matters for doorbell footage.

**Fallback (accuracy upgrade): InsightFace SCRFD + ArcFace (`buffalo_s` or `buffalo_l`) on onnxruntime, behind the same interface.**

- It is clearly more accurate: buffalo_l scores CFP-FP 99.33 against SFace's 94.80, and its detector is much better on small faces (WIDER-hard 83.05 against 75.03).
- **The pretrained packs are licensed for non-commercial research only.** freeblink.app could use them only after buying a licence from InsightFace (recognition-oss-pack@insightface.ai) ([README](https://github.com/deepinsight/insightface/blob/master/README.md#license)).
- A cloud alternative is **AWS Rekognition**, with each user bringing their own AWS key (the same model as the Claude key). It is the only big-cloud 1:N API open to self-serve signup.

**Claude cannot be the recogniser.**

- Anthropic's vision docs say: "Claude cannot be used to name people in images and refuses to do so" ([vision docs](https://platform.claude.com/docs/en/build-with-claude/vision)).
- The Usage Policy lists facial recognition under both its privacy and surveillance prohibitions ([AUP](https://www.anthropic.com/legal/aup)).
- Instead, recognise faces locally and pass Claude only a text hint ("the local matcher recognised: Mom") so the caption can use the name.

**Top 3 risks**

1. **Biometric law on the hosted service.** The hard part is the passers-by, not the enrolled family: to say "not Mom", the matcher has to compute a face template for every courier and neighbour.
   - The EDPB calls such a system unlawful unless *everyone* captured has consented, "even if their template is deleted within the shortest possible period" ([EDPB 3/2019](https://www.edpb.europa.eu/our-work-tools/our-documents/guidelines/guidelines-32019-processing-personal-data-through-video_en), Example 5).
   - Illinois BIPA has statutory damages of $1,000–$5,000 per person and no household exemption ([740 ILCS 14](https://ftp.ilga.gov/ILCS/Ch%200740/Act%200014/074000140K15.html)).
   - Ring and Google Nest both switch their features off in Illinois ([Ring](https://ring.com/support/articles/z3yhg/familiar-faces), [Nest](https://support.google.com/googlenest/answer/9268625)).
   - **Recommendation: self-host first. Keep it off on freeblink.app until there is a legal review and geofencing.**
2. **Accuracy on Blink footage.** Faces are small, angled, backlit, and grey at night under IR.
   - SFace is weakest on profiles (CPLFW 91.05%) ([OpenCV tutorial](https://github.com/opencv/opencv/blob/4.x/doc/tutorials/dnn/dnn_face/dnn_face.markdown)).
   - Frigate's docs say the models "are trained on color images and will not be able to extract features from gray-scale images" ([Frigate docs](https://docs.frigate.video/configuration/face_recognition)).
   - A wrong "Mom is at the door" is worse than "A person". Thresholds must be conservative, with quality gates and "a person" as the fallback.
3. **Footprint, provenance and policy.**
   - opencv-python-headless adds about 145 MB (arm64) to 216 MB (amd64) installed. RAM on the 1 GB E2.1.Micro is UNVERIFIED.
   - SFace's training dataset is undocumented and may be the withdrawn MS-Celeb-1M.
   - Whether passing face-matcher names to Claude is fine under Anthropic's AUP is an interpretation that Anthropic has not confirmed.

---

## 1. How this fits the current code

These are the parts of the code that a face module would touch:

- **Pipeline.** `EventWatcher._process` in [app/events.py](../../app/events.py) downloads each new clip. It then runs one of two engines:
  - `DETECT_ENGINE=claude`, the default: `_process_claude`, which sends 4 frames at 640 px to Claude Sonnet ([app/claude_vision.py](../../app/claude_vision.py));
  - `DETECT_ENGINE=yolo`: `Detector.analyze_clip` ([app/detector.py](../../app/detector.py)).

  Today the two engines are mutually exclusive. A face module has to run **alongside** Claude, before the Claude call, so the names can go into the prompt.
- **YOLO wiring, which is the pattern to copy:**
  - one process-wide `shared_detector()`;
  - the heavy import happens inside `_get_model()` on first use;
  - models are downloaded into `config.MODELS_DIR` (`/data/models`);
  - `asyncio.Lock` allows one clip at a time;
  - blocking work runs in `asyncio.to_thread`;
  - the dependency is a commented-out line in `requirements.txt`.
- **Frames.** `media.extract_frames(video, count, max_width=1280)` already returns evenly spaced JPEGs at up to 1280 px. Claude gets 640 px copies; faces need the 1280 px (or full-size) frames.
- **Clip cache.** `BlinkService.clip_video` keeps a cache of recent MP4s in the tenant folder. "Pick a face from a past clip" can re-run detection on demand, so nothing has to be stored for faces nobody enrolled.
- **Per-tenant storage.**
  - Each Blink account has `data/users/<tenant_id>/`.
  - `UserSettings` keeps the Claude key Fernet-encrypted ([app/settings.py](../../app/settings.py), [app/crypto.py](../../app/crypto.py)).
  - `delete_account` runs `rmtree` on the whole tenant folder ([app/tenants.py](../../app/tenants.py)), so face data stored there is erased automatically.
- **The prompt today forbids identity.** `SYSTEM_PROMPT` ends with "No speculation about identity or intent." That rule must stay for anything Claude infers on its own. The change is to allow names that come from the local matcher.
- **Hosting.** The hosted service uses bring-your-own-key for Claude, so each user is Anthropic's customer. A server-side face matcher would be processed *by the operator*, which changes who carries the legal duties (see section 6).

## 2. Comparison table

| Option | Kind | Owner-published accuracy | Models | Install (cp312) | arm64 + amd64 wheels | Weights licence | Public hosted OK? | Maintenance |
|---|---|---|---|---|---|---|---|---|
| **OpenCV YuNet + SFace** | Local, `opencv-python-headless` | SFace LFW 99.40 (zoo) / 99.60 (tutorial); CFP-FP 94.80; YuNet WIDER 0.884/0.866/0.750 | 0.23 MB + 38.7 MB | ~145 MB arm64 / ~216 MB amd64 incl. numpy | Yes (abi3 manylinux_2_28) | MIT + Apache-2.0 | **Yes** | Active (5.0.0.93, 2026-07-02; zoo pushed 2026-10-20) |
| **InsightFace buffalo_l** | Local, onnxruntime | LFW 99.83, CFP-FP 99.33, IJB-C 97.25; SCRFD-10G WIDER-hard 83.05 | 16.9 + 174.4 MB (skip the 143.6 MB 3D-landmark model) | ~455–521 MB (pulls GUI opencv, scipy, scikit-image) | Yes (pure wheel since 1.0) | **Non-commercial research only** | **No**, unless licensed | Active (2.1, 2026-10-03) |
| **InsightFace buffalo_s** | Local, onnxruntime | LFW 99.70, CFP-FP 98.00, IJB-C 95.02; SCRFD-500M WIDER-hard 68.51 | 2.5 + 13.6 MB | as above | Yes | **Non-commercial** | **No**, unless licensed | as above |
| face_recognition + dlib | Local | LFW 99.38 | 22.5 MB (+ 5-point landmarks) | Compiles dlib (sdist only) | No (dlib needs cmake/C++) | CC0 (except the 68-point model, which is non-commercial) | Licence OK; practical no | Stale (1.3.0, 2020) |
| DeepFace | Local wrapper | Depends on the wrapped model | Varies | ~268–351 MB | Yes | Inherits each model's licence | Only with permissive models | Active (0.0.103, 2026-10-20) |
| MediaPipe BlazeFace | Local, **detection only** | — (2.94 ms on Pixel 6) | 0.23 MB | ~410–483 MB | Yes | Apache-2.0, "consented images" | Detector only | Active |
| facenet-pytorch | Local, PyTorch | LFW 99.65 (VGGFace2) | 107 MB | torch <2.3 (amd64 wheel 755 MB) | Yes, but huge | Dataset terms UNVERIFIED | Unclear | Stale (2.6.0, 2024-04) |
| CompreFace | Self-hosted service (5 containers) | FaceNet LFW 99.63 | — | Docker, needs AVX | **amd64 only** | FaceNet default; InsightFace builds are non-commercial | Not viable | Dormant (v1.2.0, 2023-08) |
| Ultralytics / YOLO-face | Local, PyTorch | yolov8n-face WIDER-hard 79.6 (3rd party) | 6 MB | ~1 GB | Yes | AGPL-3.0 / GPL-3.0 | **Avoid** (copyleft over network) | Active |
| EdgeFace (Idiap) | Local (PyTorch → ONNX) | LFW 99.73–99.78, CFP-FP 94.7–95.7 | 5–15 MB | onnxruntime ~56–65 MB | Yes (via onnxruntime) | BSD-3 weights; training data WebFace260M (research only?) | Unclear | — |
| **AWS Rekognition** | Cloud 1:N collections | Not published as LFW etc. (UNVERIFIED) | — | `boto3` | n/a | AWS Service Terms | Yes, with notice-and-consent duties | Active |
| Azure AI Face | Cloud | — | — | — | n/a | Limited Access | **Closed** to FreeBlink | Active, gated |
| Google Cloud Vision | Cloud | — | — | — | n/a | — | **No identification** | — |
| Claude (vision) | LLM | — | — | — | n/a | AUP | **Not allowed** to name people | — |

Sources for every cell are in the per-option sections below.

## 3. Per-option notes

### 3.1 OpenCV YuNet + SFace (primary pick)

**Package**
- `opencv-python-headless` 5.0.0.93, uploaded 2026-07-02, Apache-2.0 ([PyPI](https://pypi.org/pypi/opencv-python-headless/json)).
- `cp37-abi3-manylinux_2_28` wheels for aarch64 (39.6 MB) and x86_64 (61.2 MB). Installed with numpy: about 145 MB on arm64 and about 216 MB on amd64.
- `cv2.FaceDetectorYN` and `cv2.FaceRecognizerSF` are present in 5.0.0.93; the research agent checked this with a local install.

**YuNet (detection)** ([model folder](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet))
- MIT licence ("All files in this directory are licensed under MIT License").
- Size: about 230 KB, or 100 KB as int8. A `2026may` version with dynamic input shape is now the default.
- Accuracy: WIDER Face val 0.884 / 0.866 / 0.750 (easy / medium / hard).
- Handles faces of roughly 10×10 to 300×300 px.
- Returns 5 landmarks, which SFace uses for alignment.

**SFace (recognition)** ([model folder](https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface))
- Apache-2.0 licence.
- Model: MobileFaceNet trained with the SFace loss ([paper](https://arxiv.org/abs/2205.12010)).
- Size: 38.7 MB fp32 or 9.9 MB int8. Prefer fp32; an open issue reports poor int8 scores ([opencv_zoo #189](https://github.com/opencv/opencv_zoo/issues/189)).
- Accuracy (OpenCV's own table, [tutorial](https://github.com/opencv/opencv/blob/4.x/doc/tutorials/dnn/dnn_face/dnn_face.markdown)):

  | Benchmark | Accuracy | Cosine threshold |
  |---|---|---|
  | LFW | 99.60% | 0.363 |
  | CALFW | 93.95% | |
  | CPLFW (cross-pose) | 91.05% | |
  | AgeDB-30 | 94.90% | |
  | CFP-FP (frontal vs profile) | 94.80% | |

  The model zoo reports LFW 0.9940.

**CPU speed** (owner's [benchmark](https://github.com/opencv/opencv_zoo/blob/main/benchmark/README.md))

| Hardware | YuNet at 160×120 | SFace per face |
|---|---|---|
| Intel i7-12700K | 0.69 ms | 5.09 ms |
| Raspberry Pi 4B (Cortex-A72) | 6.23 ms | 68.82 ms |

No Ampere A1 numbers are published. My extrapolation, **UNVERIFIED**: detection on a 1280×720 frame scales roughly with pixel count, about 48× the 160×120 input. That gives roughly 30–300 ms per frame on an A1 core, plus about 10–70 ms per face. A 10-frame clip should take well under 5 s, which fits the latency budget.

**Weaknesses**
- Lower accuracy on profiles than ArcFace-R50.
- The training dataset of the zoo ONNX is not documented. The SFace paper trains on CASIA-WebFace, VGGFace2 and MS-Celeb-1M ([repo](https://github.com/zhongyy/SFace)), and the SFace GitHub repo has no licence file. Provenance is therefore a residual risk even though the weights carry an Apache-2.0 grant from OpenCV.

**Fit:** best overall. One small dependency, which may already be present if a self-hoster installed ultralytics. Simple API, ARM proven by the owner's Raspberry Pi numbers, clean licence.

### 3.2 InsightFace (SCRFD + ArcFace) via onnxruntime (fallback)

**Package and repo**
- `insightface` 2.1, uploaded 2026-10-03; since 1.0 it is a pure `py3-none-any` wheel ([PyPI](https://pypi.org/pypi/insightface/json), [CHANGELOG](https://github.com/deepinsight/insightface/blob/master/python-package/CHANGELOG.md)).
- Repo is active, last pushed 2026-10-19.
- Dependencies pull GUI `opencv-python`, scipy and scikit-image (about 455–521 MB installed). You can avoid that by loading the ONNX files directly with `onnxruntime` and doing the 5-point alignment yourself.

**onnxruntime**
- 1.31.1 (2026-10-26), MIT.
- cp312 manylinux_2_28 wheels: aarch64 21.4 MB, x86_64 23.8 MB, about 56–65 MB installed ([PyPI](https://pypi.org/pypi/onnxruntime/json)).

**Accuracy** ([model zoo](https://github.com/deepinsight/insightface/blob/master/python-package/docs/model_zoo.md))

| Pack | LFW | CFP-FP | AgeDB-30 | IJB-C (E4) |
|---|---|---|---|---|
| buffalo_l (R50, WebFace600K) | 99.83 | 99.33 | 98.23 | 97.25 |
| buffalo_s (MobileFaceNet) | 99.70 | 98.00 | 96.58 | 95.02 |

SCRFD detector on WIDER Face (easy / medium / hard) ([SCRFD README](https://github.com/deepinsight/insightface/blob/master/detection/scrfd/README.md)):
- SCRFD-10G: 95.16 / 93.87 / 83.05
- SCRFD-500M: 90.57 / 88.12 / 68.51

SCRFD-0.5GF runs in 28.3 ms at 640×480 on one Ryzen 9 3950X thread (same README).

**Licence (the blocker)**
- The code is MIT.
- The models are not. The README says: "The training data containing the annotation (and the models trained with these data) are available for non-commercial research purposes only. Both manual-downloading models from our github repo and auto-downloading models with our python-library follow the above license policy" ([README](https://github.com/deepinsight/insightface/blob/master/README.md#license)).
- The new packs ship a `MODEL.LICENSE` stating "public packages carry a non-commercial grant" ([model_zoo.md](https://github.com/deepinsight/insightface/blob/master/python-package/docs/model_zoo.md)).
- Commercial licences: "please contact recognition-oss-pack@insightface.ai".
- Even self-hosted home-security use is arguably not "research". **For FreeBlink, treat it as licence-required everywhere.**

### 3.3 face_recognition (ageitgey) + dlib

**Status:** `face_recognition` 1.3.0 is from 2020-02-20 ([PyPI](https://pypi.org/pypi/face-recognition/json)), so it is effectively unmaintained.

**Install pain**
- `dlib` 20.0.1 ships **sdist only**, so it needs cmake and a C++ toolchain on both architectures ([PyPI](https://pypi.org/pypi/dlib/json)).
- The third-party `dlib-bin` package has wheels, but `face_recognition` depends on `dlib`, not `dlib-bin`.

**Models** ([dlib-models](https://github.com/davisking/dlib-models))
- Released CC0 ("released them into the public domain").
- The ResNet model scores LFW 99.38%. It was trained partly on "images I scraped from the internet".
- The 68-point landmark model is non-commercial (from iBUG 300-W). `face_encodings` defaults to the 5-point model ([api.py](https://github.com/ageitgey/face_recognition/blob/master/face_recognition/api.py)).

**Ecosystem signal:** Home Assistant deprecated (2025-05-28, [PR #145767](https://github.com/home-assistant/core/pull/145767)) and then removed (2025-10-29, [PR #155450](https://github.com/home-assistant/core/pull/155450)) its dlib face integrations, because they need system packages that can't be installed on supported installs.

**Verdict:** not recommended.

### 3.4 DeepFace

- **Version:** 0.0.103, uploaded 2026-10-20, MIT ([PyPI](https://pypi.org/pypi/deepface/json)).
- **Runtime:** onnxruntime is now the default backend ([README](https://github.com/serengil/deepface)).
- **Licensing:** it is a convenience wrapper over VGG-Face, FaceNet, ArcFace, SFace, Buffalo_L, YuNet, YOLO and others. Its README says "License types will be inherited when you intend to utilize those models", so it adds no licensing clarity.
- **Footprint:** about 268–351 MB (pandas, gdown, opencv-contrib, retina-face).
- **Verdict:** no advantage over calling OpenCV directly.

### 3.5 MediaPipe face detection (BlazeFace)

- **Version:** `mediapipe` 1.2.2, uploaded 2026-11-03, Apache-2.0 ([PyPI](https://pypi.org/pypi/mediapipe/json)).
- **Detection only:** it outputs boxes and 6 keypoints, with no embeddings ([docs](https://developers.google.com/edge/mediapipe/solutions/vision/face_detector)).
- **Model card** ([PDF](https://storage.googleapis.com/mediapipe-assets/MediaPipe%20BlazeFace%20Model%20Card%20(Short%20Range).pdf)):
  - "trained and evaluated on consented images";
  - "Any form of surveillance or identity recognition is explicitly out of scope";
  - short range only (about 2 m).
- **Footprint:** about 410–483 MB installed.
- **Verdict:** wrong tool for doorbells.

### 3.6 facenet-pytorch

- **Status:** 2.6.0 from 2024-04-29, with pinned dependencies `torch<2.3`, `numpy<2` ([PyPI](https://pypi.org/pypi/facenet-pytorch/json)).
- **Weights:** InceptionResnetV1 ported from [davidsandberg/facenet](https://github.com/davidsandberg/facenet), scoring LFW 99.65 (VGGFace2) ([README](https://github.com/timesler/facenet-pytorch)).
- **Licence:** there is no separate weights licence, and the dataset terms are UNVERIFIED.
- **Verdict:** stale, PyTorch-heavy, and the licence is unclear. Skip.

### 3.7 CompreFace (self-hosted service)

- **Status:** Apache-2.0. v1.2.0 from 2023-08-22, last commit 2023-11-14 ([releases](https://github.com/exadel-inc/CompreFace/releases)), so it is dormant.
- **Hardware:** needs "x86 processor and AVX support" ([README](https://github.com/exadel-inc/CompreFace)), and the core images are amd64 only.
- **Resources:** runs five containers, including Postgres and a Java API whose dev configuration sets `-Xmx8g`.
- **Models:** the default build is FaceNet. The InsightFace builds inherit the non-commercial weights ([custom-builds](https://github.com/exadel-inc/CompreFace/blob/master/custom-builds/README.md)).
- **Verdict:** not viable on Oracle free tier.

### 3.8 Ultralytics / YOLO face

- **Licence:** `ultralytics` 8.4.202 is AGPL-3.0, and commercial use requires an Enterprise License ([PyPI](https://pypi.org/pypi/ultralytics/json)). AGPL §13 obliges you to offer source to network users of a modified version ([AGPL](https://www.gnu.org/licenses/agpl-3.0.html)).
  - Note: the existing optional `DETECT_ENGINE=yolo` already carries this.
- **No face model from Ultralytics:** no official face model was found (UNVERIFIED as an absence).
- **Third-party face weights:** [derronqi/yolov8-face](https://github.com/derronqi/yolov8-face) and [akanametov/yolo-face](https://github.com/akanametov/yolo-face) are GPL-3.0.
- These are detectors only. You would still need a recogniser.
- **Verdict:** avoid for faces.

### 3.9 Other permissive-looking options

- **EdgeFace** ([repo](https://github.com/otroshi/edgeface)):
  - The BSD-3 licence covers the checkpoints.
  - Models are 5–15 MB and score LFW about 99.7.
  - The weights are PyTorch, so an ONNX export is needed.
  - The training data is reportedly WebFace260M (research-only terms; UNVERIFIED).
  - Possible future accuracy upgrade, provenance unclear.
- **AdaFace:** MIT code, but its checkpoints are trained on MS1M and WebFace data ([repo](https://github.com/mk-minchul/AdaFace)). Unclear.
- **uniface** (MIT wrapper): its own table marks SCRFD and ArcFace as non-commercial ([licence table](https://yakhyo.github.io/uniface/license-attribution/)).

### 3.10 Cloud APIs

**AWS Rekognition face collections: works, self-serve.**

How it works:
- `IndexFaces` stores face vectors, not images ([docs](https://docs.aws.amazon.com/rekognition/latest/dg/collections.html)).
- Search with `SearchFacesByImage`, or with `SearchUsersByImage` against "user vectors" that group up to 100 faces per person. AWS says this "can significantly improve accuracy" compared with searching individual face vectors.

Pricing ([pricing](https://aws.amazon.com/rekognition/pricing/); the region for these prices is UNVERIFIED):
- About $0.001 per image.
- $0.00001 per stored face vector per month.
- Free tier: 1,000 images per month for 12 months, or credits for accounts created after 2025-07-15.

Terms ([AWS Service Terms](https://aws.amazon.com/service-terms/), [Biometric Notice and Consent Terms](https://aws.amazon.com/legal/biometric-notice-and-consent-terms/)):
- §50.3: AWS may use content to improve the service unless you set an AWS Organizations opt-out policy.
- §50.9.2: AWS acts as processor for face vectors.
- The Biometric Notice and Consent Terms require the customer to give notice and obtain "informed written consent and explicit consent, where required" from "all relevant data subjects", naming BIPA, CUBI, Washington law, CCPA and GDPR.

Status: no deprecation of collections. Only People Pathing ended, and the streaming and bulk features closed to new customers ([availability changes](https://docs.aws.amazon.com/rekognition/latest/dg/rekognition-availability-changes.html)).

Fit for FreeBlink:
- Only as **bring-your-own AWS credentials**, so the user is the AWS customer.
- A clip with several faces needs a detect call plus one search per face (about $0.002–0.01 per clip, my estimate).
- Every passer-by's face goes to AWS. That makes the consent problem worse, not better.

**Azure AI Face: closed to us.**
- Identify and Verify are Limited Access and need registration ([Limited Access](https://learn.microsoft.com/en-us/azure/foundry/responsible-ai/computer-vision/limited-access-identity)).
- Microsoft's general Limited Access page ([page](https://learn.microsoft.com/en-us/azure/ai-services/cognitive-services-limited-access)) says access is "available only to customers managed by Microsoft", that "Registration forms submitted with personal email addresses are denied", and that "We aren't able to accept requests to become a managed customer at this time".
- The approved use cases do not include home security cameras, and Microsoft warns against "ongoing surveillance" and "public spaces" ([transparency note](https://learn.microsoft.com/en-us/azure/ai-foundry/responsible-ai/face/transparency-note)).

**Google Cloud Vision: detection only.** "Specific individual Facial Recognition is not supported" ([docs](https://docs.cloud.google.com/vision/docs/detecting-faces)). Celebrity recognition was deprecated after 2025-09-16 ([docs](https://docs.cloud.google.com/vision/docs/celebrity-recognition)).

**Others**
- **Luxand.cloud:** from $9/month ([pricing](https://luxand.cloud/pricing)). 1:N is described only in a blog post (UNVERIFIED in the API reference). Retention and location are not stated.
- **Face++ (Megvii):** Megvii is on the US Entity List ([eCFR](https://www.ecfr.gov/api/search/v1/results?query=Megvii&date=2026-10-30)). Avoid.
- **Kairos:** 1:N self-serve not confirmed ([pricing](https://www.kairos.com/pricing)).
- **Clarifai:** a do-it-yourself KNN workflow, not a turnkey identify call ([docs](https://docs.clarifai.com/create/workflows/examples/knn-classifier/)).

### 3.11 Claude (LLM-based identification)

**Not allowed, and not needed.**

- The [vision docs](https://platform.claude.com/docs/en/build-with-claude/vision) list under limitations: "**People identification:** Claude cannot be used to name people in images and refuses to do so (see the Usage Policy)."
- The [Usage Policy](https://www.anthropic.com/legal/aup) (the page currently shows effective date Nov 12, 2026) has two relevant sections:
  - "Do Not Compromise Privacy or Identity Rights" prohibits misusing or collecting without permission "biometric or neural data (including facial recognition)".
  - The surveillance section says "This includes using our products for facial recognition…".
- So sending reference photos of Mom alongside the frames and asking Claude "is this Mom?" is out. Expect refusals, and it would be a policy violation on each user's API key.

**What is reasonable:**
- Do the biometric matching locally, then give Claude a **text** hint: "A separate face matcher recognised: Mom (score 0.62, frame 3)."
- Claude then writes "Mom arrives at the front door with groceries." It never sees reference photos and does no identification itself.
- This is my reading of the policy and has not been confirmed by Anthropic. See the Open questions.
- The fully conservative alternative: don't send names to Claude at all, and prefix the name to Claude's caption in code ("Mom · Person at the door").

### 3.12 Reference designs from the home-camera ecosystem

**Frigate NVR** (MIT) has had built-in face recognition since 0.16.0 (2025-08-16). The latest release is v0.18.2 (2026-10-15), and 0.18.0 added "Face recognition optimizations to increase accuracy and reject poor candidates" ([releases](https://github.com/blakeblackshear/frigate/releases)).

Models, from source at v0.18.2 ([face.py](https://github.com/blakeblackshear/frigate/blob/v0.18.2/frigate/data_processing/real_time/face.py), [model.py](https://github.com/blakeblackshear/frigate/blob/v0.18.2/frigate/data_processing/common/face/model.py)):
- **Detection:** the same OpenCV **YuNet** (`cv2.FaceDetectorYN`, score 0.5) we recommend, run only inside detected `person` boxes.
- **Alignment:** OpenCV FacemarkLBF landmarks.
- **Recognition:**
  - `small` (the default) is **FaceNet** on CPU: "optimized for efficiency and is not as accurate";
  - `large` is ArcFace, "only recommended to be run when an integrated or dedicated GPU / NPU is available" ([docs](https://docs.frigate.video/configuration/face_recognition)).
- **CPU requirement:** "A CPU with AVX + AVX2 instructions is required". That is an x86-only requirement, so Frigate's own face feature is not offered on ARM (my inference).
- **Weights origin:** downloaded from the `NickM-27/facenet-onnx` GitHub release (Apache-2.0 repo); the upstream training provenance is undocumented (UNVERIFIED).

Matching ([model.py](https://github.com/blakeblackshear/frigate/blob/v0.18.2/frigate/data_processing/common/face/model.py)):
- Each person gets a mean embedding with outlier trimming (`scipy.stats.trim_mean`).
- The cosine score is mapped to a confidence with a sigmoid, minus a blur penalty.
- The final label requires "consistent results across many frames, weighted by face area".

Defaults ([config](https://raw.githubusercontent.com/blakeblackshear/frigate/v0.18.2/frigate/config/classification.py), [docs](https://docs.frigate.video/configuration/face_recognition)):
- `min_area` 750 px (face box area)
- `unknown_score` 0.8
- `recognition_threshold` 0.9
- the feature is off by default

Enrollment UX (same docs):
- An "Add Face" wizard (1–5 frontal photos), then a **Train** tab of recent detected faces with their scores; the user assigns them to people.
- Recommended images per person: 5–10 minimum, 20–30 recommended, 50–100 ideal.
- Don't bulk-import a photo library.
- **Avoid IR/grayscale images.** Add night shots only sparingly, once colour recognition is stable.

Lessons for FreeBlink:
- Use the same detector.
- Use a multi-frame vote and a quality gate.
- Enroll from the camera's own footage.
- Unlike Frigate, **don't keep a gallery of every unknown face** (see the legal section).

**Double Take** (MIT) ([repo](https://github.com/jakowenko/double-take)):
- A UI that fans Frigate snapshots out to CompreFace, DeepStack, Facebox or AWS Rekognition.
- Effectively dormant: v1.13.1 came out in 2022-10, and v1.13.2 (2025-08-18) was a release chore.
- Frigate says it conflicts with Frigate's built-in feature.

**Home Assistant**
- dlib face integrations removed in 2025 (PRs above).
- `microsoft_face` removed 2026-06-27, "broken since 2022" ([PR #174977](https://github.com/home-assistant/core/pull/174977)).
- Today people use Frigate sub-labels or the HACS LLM Vision integration (v1.7.2, 2026-09-03; [repo](https://github.com/valentinfrlch/ha-llmvision)).

**Commercial vendors** all make the feature opt-in, push consent onto the camera owner, and switch it off in Illinois:

| Vendor | Feature | Where it is off | Other notes |
|---|---|---|---|
| Google Nest | Familiar face detection | Illinois | [help](https://support.google.com/googlenest/answer/9268625) |
| Ring | Familiar Faces | Illinois, Texas, Portland OR, Quebec | Unnamed faces deleted after 30 days; at most 50 profiles ([help](https://ring.com/support/articles/z3yhg/familiar-faces)). Launched 2025-12-09 with the example "Mom at Front Door" ([blog](https://blog.ring.com/products-innovation/introducing-familiar-faces-a-new-way-to-see-whos-at-your-front-door/)) |
| Arlo | Person Recognition | Illinois, Texas, Quebec | [KB](https://kb.arlo.com/000063157/) |
| Wyze | Friendly Faces | Illinois "strictly prohibited" | Personal or household use only; unassigned faces deleted within 30 days ([terms](https://www.wyze.com/pages/terms-of-service-friendly-faces)) |

## 4. Licensing table

| Option | Code licence | Weights licence | Training-data note | Free public hosted (freeblink.app) | Self-host |
|---|---|---|---|---|---|
| OpenCV + YuNet + SFace | Apache-2.0 ([PyPI](https://pypi.org/pypi/opencv-python-headless/json)) | YuNet MIT ([LICENSE](https://github.com/opencv/opencv_zoo/blob/main/models/face_detection_yunet/LICENSE)); SFace Apache-2.0 ([LICENSE](https://github.com/opencv/opencv_zoo/blob/main/models/face_recognition_sface/LICENSE)) | SFace dataset undocumented; may include MS-Celeb-1M (withdrawn 2019) | **Safe** (residual provenance risk) | Safe |
| InsightFace buffalo_l/s, antelopev2 | MIT | **Non-commercial research only**; commercial licence by email ([README](https://github.com/deepinsight/insightface/blob/master/README.md#license)) | WebFace600K / Glint360K | **Not without a paid licence** | Grey: home use is not "research"; treat as licence-required |
| dlib / face_recognition | Boost / MIT | CC0, except the 68-point model, which is non-commercial ([dlib-models](https://github.com/davisking/dlib-models)) | Partly scraped from the web | OK if you avoid the 68-point model | OK |
| DeepFace | MIT | Inherits the chosen model's licence | — | Only with permissive models | Same |
| MediaPipe BlazeFace | Apache-2.0 | Apache-2.0; the card scopes out identity recognition | "consented images" | Detector only | Same |
| facenet-pytorch | MIT | No separate licence | VGGFace2 / CASIA terms UNVERIFIED | Unclear | Unclear |
| CompreFace | Apache-2.0 | FaceNet default; InsightFace builds are non-commercial | — | Not viable (amd64, heavy) | amd64 only |
| Ultralytics / YOLO-face | AGPL-3.0 / GPL-3.0 | AGPL / GPL | — | Avoid (network copyleft) | OK if AGPL obligations are met |
| EdgeFace | BSD-3 | BSD-3 | WebFace260M (research terms, UNVERIFIED) | Unclear | Probably OK |
| AWS Rekognition | Proprietary service | — | — | Yes, with BYO key and consent duties | Yes |
| Azure Face | Proprietary | — | — | Gated / closed | Gated |
| Claude | — | — | — | AUP prohibits facial recognition | Same |

## 5. Legal and privacy (not legal advice)

**The main issue.** Matching "Mom" against a gallery means computing a face template for *every* face in every clip: couriers, neighbours, children walking past. The enrolled family can consent. The passers-by cannot.

### EU GDPR

**Biometric data is special-category data**
- A face embedding used for matching is biometric data: data from "specific technical processing … which allow or confirm the unique identification" of a person (Art. 4(14)).
- Processing it "for the purpose of uniquely identifying a natural person" is prohibited unless an Art. 9(2) exception applies (Art. 9(1)). In practice that means **explicit consent**, Art. 9(2)(a) ([GDPR](https://eur-lex.europa.eu/eli/reg/2016/679/oj)).
- Raw photos are biometric only when processed that way (Recital 51).

**The household exemption is narrow**
- It is lost when a camera covers "even partially, a public space" (*Ryneš*, C-212/13, para 33) ([judgment](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:62013CJ0212)). A doorbell facing a street is outside it.
- Even when a user's own use is exempt, the GDPR still "applies to controllers or processors which provide the means for processing personal data for such personal or household activities" (Recital 18). **That covers FreeBlink's operator.**

**EDPB Guidelines 3/2019 on video devices** ([guidelines](https://www.edpb.europa.eu/our-work-tools/our-documents/guidelines/guidelines-32019-processing-personal-data-through-video_en), [PDF](https://www.edpb.europa.eu/sites/default/files/files/file1/edpb_guidelines_201903_video_devices_en_0.pdf)):
- Private biometric recognition "will, in most cases, require explicit consent from all data subjects" (para 77).
- An Art. 9(2) exception "is still needed for anyone captured by the camera" (para 84).
- **Example 5:** the system is unlawful if it templates non-consenting passers-by "even if their template is deleted within the shortest possible period".
- **Example 6** (hotel VIP alerts, which is structurally FreeBlink): unlawful "unless all other guests monitored … have consented".
- Safeguards:
  - delete no-match templates immediately (para 79);
  - offer a non-biometric alternative (para 86);
  - encrypt templates and store them separately from identities (§5.2).

**Other national regulators**
- The Dutch DPA accepts household-exempt face recognition only when templates stay on the user's own device, encrypted ([AP](https://www.autoriteitpersoonsgegevens.nl/en/themes/identification/biometrics/facial-recognition)). A server-side store does not fit.
- The Irish DPC treats a doorbell that covers the footpath as outside the household exemption ([DPC leaflet](https://www.dataprotection.ie/sites/default/files/uploads/2026-05/Domestic_CCTV_A5_ENG_Final.pdf)).

**Obligations for a hosted service available in the EU**
- Art. 3(2): the GDPR applies to a non-EU operator offering services to people in the EU, even for free.
- Decide controller or processor (Art. 26/28; [EDPB 07/2020](https://www.edpb.europa.eu/our-work-tools/our-documents/guidelines/guidelines-072020-concepts-controller-and-processor-gdpr_en)).
- A DPIA (Art. 35(3)(b)/(c)).
- An Art. 27 EU representative if the operator is not established in the EU.

**Pending change:** a "Digital Omnibus" proposal would add a GDPR exception for 1:1 *verification* under the data subject's sole control. It would not cover 1:N identification of passers-by, and it is not adopted (UNVERIFIED secondary source: [EU internet policy blog](https://euinternetpolicy.wordpress.com/2026/10/23/the-councils-digital-omnibus-mandate-what-it-would-change-in-the-gdpr/)).

### EU AI Act (Regulation 2024/1689)

Text: [EUR-Lex](https://eur-lex.europa.eu/eli/reg/2024/1689/oj). Wording was checked via the [artificialintelligenceact.eu](https://artificialintelligenceact.eu) mirror because EUR-Lex blocked automated fetching.

**Definitions**
- "Remote biometric identification system" (Art. 3(41)): identifies people "without their active involvement, typically at a distance" by comparison with a reference database.
- *Biometric verification* (one-to-one, Art. 3(36)) is excluded from the high-risk listing in Annex III point 1(a).

**Who is covered**
- Homeowners using it purely personally are exempt as **deployers** (Art. 2(10)).
- The **provider**, meaning whoever develops it and puts it into service, which for freeblink.app is the operator, is not exempt.
- The open-source exemption (Art. 2(12)) does not apply to high-risk systems put into service.

**Is FreeBlink's feature high-risk?** A hosted "known faces" 1:N matcher plausibly fits the remote-biometric-identification definition and so would be high-risk under Annex III. The counter-arguments are the small private gallery and the fact that "typically at a distance" is not a hard criterion. No Commission guidance specific to doorbells was found (UNVERIFIED either way).

**Timing**
- Art. 5 prohibitions have applied since 2 Feb 2025.
- Art. 5(1)(e) bans building facial-recognition databases through "untargeted scraping of facial images from … CCTV footage". So **don't build an automatic gallery of unknown faces.**
- The AI Omnibus moved Annex III high-risk obligations to **2 Dec 2027** ([Commission](https://digital-strategy.ec.europa.eu/en/policies/regulatory-framework-ai), [Council press release](https://www.consilium.europa.eu/en/press/press-releases/2026/06/29/artificial-intelligence-council-gives-final-green-light-to-simplify-and-streamline-rules/)). The regulation number was not confirmed on EUR-Lex (UNVERIFIED).

### UK

- The ICO's biometric recognition guidance says explicit consent is usually the appropriate Art. 9 condition. Without one, "you are infringing data protection law" ([ICO](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/lawful-basis/biometric-data-guidance-biometric-recognition/how-do-we-process-biometric-data-lawfully/)).
- Doorbells that uniquely identify people are "likely" to process special-category data ([ICO smart doorbells](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/cctv-and-video-surveillance/guidance-on-video-surveillance-including-cctv/additional-considerations-for-technologies-other-than-cctv/smart-doorbells-commercial-use/)).
- Home CCTV that captures beyond your boundary is within data protection law ([ICO home CCTV](https://ico.org.uk/for-the-public/home-cctv-systems/)).

### Illinois BIPA (740 ILCS 14)

**Definitions** (Sec. 10)
- A "biometric identifier" includes a "scan of … face geometry"; photographs are excluded.
- A "private entity" includes "any individual", so there is **no household or non-commercial exemption** ([P.A. 103-769](https://ftp.ilga.gov/Public%20Acts/103/103-0769.htm)).

**Duties** (Sec. 15, [text](https://ftp.ilga.gov/ILCS/Ch%200740/Act%200014/074000140K15.html))
- (a) a public written retention and destruction policy; destroy data when its purpose is satisfied or within 3 years of the last interaction;
- (b) written notice, the purpose and the retention term, plus a **written release before collection**;
- (c) and (d) no selling or disclosure;
- (e) reasonable security.

**Damages and courts**
- Damages are $1,000 per negligent violation and $5,000 per intentional or reckless violation, plus fees.
- The 2024 amendment (P.A. 103-769) made repeated scans of one person a single violation and allowed electronic signatures.
- No actual harm is needed (*Rosenbach v. Six Flags*, 2019 IL 123186, [opinion](https://www.illinoiscourts.gov/Opinions/SupremeCourt/2019/123186.pdf)).

**Precedents from photo services**
- Facebook: $650M (2021).
- Google Photos: $100M (2022). Its class included people merely *appearing* in photos.
- Both are secondary sources: [Knobbe](https://www.knobbe.com/blog/judge-allows-facebook-settle-facial-scanning-suit-650-million), [Bursor](https://www.bursor.com/result/100-million-google-photos-settlement/).

**The "compute and discard" argument**
- *Zellmer v. Meta*, 104 F.4th 1117 (9th Cir. 2024) ([opinion](https://cdn.ca9.uscourts.gov/datastore/opinions/2024/06/17/22-16925.pdf)) confirmed that BIPA protects non-users.
- It also held that Facebook's short-lived "face signatures" of non-users were not biometric identifiers, because on that record they could not identify anyone ([Perkins Coie summary](https://perkinscoie.com/insights/update/biometric-identifiers-must-identify-ninth-circuit-clarifies-scope-bipa)).
- It is persuasive only. It binds neither Illinois courts nor anything beyond the Ninth Circuit, and it does not help with stored unknown-face embeddings.
- I could not re-extract the exact quotes from the PDF myself.

**Practical upshot:** the major vendors simply disable the feature in Illinois.

### Texas CUBI (Bus. & Com. Code §503.001)

([text](https://tcss.legis.texas.gov/resources/BC/htm/BC.503.htm))
- Covers capturing a "record of … face geometry" for a commercial purpose.
- Requires notice and consent **before** capture.
- Data must be destroyed within 1 year after the purpose expires.
- Up to $25,000 per violation, with **AG enforcement only**.
- The 2026 TRAIGA amendments added a security and fraud exemption (e)(3). Whether a home-security face matcher fits it is untested.
- Enforcement record: Meta $1.4B ([AG](https://www.texasattorneygeneral.gov/news/releases/attorney-general-ken-paxton-secures-14-billion-settlement-meta-over-its-unauthorized-capture)) and Google $1.375B ([AG](https://www.texasattorneygeneral.gov/news/releases/attorney-general-ken-paxton-finalizes-historic-settlement-google-and-secures-1375-billion-big-tech)).

### Washington

- **RCW 19.375** excludes "data generated" from photos and video, and covers only enrolment for a commercial purpose. Low risk ([RCW 19.375](https://app.leg.wa.gov/rcw/default.aspx?cite=19.375)).
- **My Health My Data Act (RCW 19.373)** treats facial imagery from which a template can be extracted as consumer health data. It requires consent by a "clear affirmative act" and allows private suits via the Consumer Protection Act ([RCW 19.373](https://app.leg.wa.gov/rcw/default.aspx?cite=19.373)). Whether an individual operator is a "regulated entity" is unclear.

### Other US laws, one line each

- **Colorado HB24-1130** (effective 2025-07-01): consent, a public retention policy, and deletion within 24 months of the last interaction at most ([bill](https://leg.colorado.gov/bills/hb24-1130)).
- **California CCPA/CPRA:** biometric data for identification is "sensitive personal information", but the law applies only to for-profit businesses above its thresholds ([§1798.140](https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=CIV&sectionNum=1798.140)).
- **Portland, OR:** bans private-entity face recognition in places of public accommodation; residences are excluded, but Ring still disables its feature there ([City Code 34.10](https://www.portland.gov/code/34/10)).

### What this means for FreeBlink

1. **Self-hosted, single household.** The operator is the homeowner. The AI Act deployer exemption applies, and the GDPR household exemption applies *only* if the camera does not cover public space. BIPA still applies to an Illinois individual.
   - FreeBlink-the-project ships code, not a service.
   - Low risk for the project. Document clearly that the self-hoster is responsible.
2. **Hosted freeblink.app.** The operator processes every visitor's face for every user.
   - EU/UK: likely unlawful for passers-by under the EDPB view, plus GDPR paperwork, plus AI Act provider duties from Dec 2027.
   - Illinois: statutory damages with no harm requirement.
   - **Do not enable it on the hosted service** until there is (a) a legal review, (b) geofencing (at least EU/EEA, UK, Illinois, Texas, Portland and Quebec, as Ring does), and (c) consent flows and a published retention policy.
3. **Design rules either way:**
   - Opt-in per account, with "a person" alerts as the non-biometric default.
   - Enroll only people who have consented, with the user attesting to it. In strict jurisdictions, the enrolled person's own electronic consent.
   - **Never store or cluster embeddings of unknown faces.** Compute them in memory, compare, and discard.
   - Encrypt enrolled embeddings.
   - Delete on opt-out, on person removal, on account deletion, and on a schedule (at most 1 year after last match satisfies the Texas, Colorado and BIPA limits).
   - Publish a short biometric retention policy.
   - Never send face data to third parties. This also argues against AWS.

## 6. Architecture proposal

### 6.1 Where it runs

- Locally in the FreeBlink process, in a new `app/faces.py`, modelled on `app/detector.py`:
  - a process-wide `shared_face_engine()`;
  - lazy `import cv2`;
  - models downloaded on first use into `config.MODELS_DIR`, from pinned opencv_zoo URLs with SHA-256 checks;
  - an `asyncio.Lock` so only one clip runs at a time;
  - work in `asyncio.to_thread`.
- One model instance serves all tenants. Galleries are per tenant.
- The engine can sit behind a small interface (`detect(frame) -> faces`, `embed(frame, face) -> vector`) so InsightFace can be swapped in later if licensed.

### 6.2 Configuration

| Variable | Default | Purpose |
|---|---|---|
| `FACES_ENABLED` | `false` | Operator switch. The hosted instance stays off. |
| `FACES_MODEL` | `sface-2021dec` | Allows an InsightFace or EdgeFace engine later. |
| `FACES_THRESHOLD` | — | Match threshold. |
| `FACES_MIN_SIZE` | — | Minimum face size. |
| `FACES_FRAMES` | — | Frames sampled per clip. |

- **Dependency:** a commented-out `opencv-python-headless==5.0.0.93` line in `requirements.txt` (the same pattern as ultralytics), plus a Docker build argument such as `--build-arg EXTRAS=faces`, or a separate `:faces` image tag. The default image then does not grow by about 150–220 MB.
- **Watch out:** ultralytics pulls GUI `opencv-python`. Installing both opencv packages causes conflicts, so document "one or the other".
- **Per-account opt-in:** a toggle in Settings that is only visible when `FACES_ENABLED`, behind a consent screen.

### 6.3 Clip pipeline

These changes go in `EventWatcher._process_claude`, before `analyzer.analyze_clip`:

1. **Extract frames.** Take about 8–12 frames at 1280 px from the clip that is already downloaded. Add a variant of `extract_frames(video, n, max_width=1280)`, or extract once and reuse the frames for Claude, which currently calls it internally. Full 1080p could help with distant faces at a CPU cost.
2. **Detect.** Run YuNet with `setInputSize` set to the frame size and a score of at least 0.8 (Frigate uses 0.5 and then filters).
3. **Apply quality gates before embedding:**
   - face box at least about 40 px wide (`FACES_MIN_SIZE`; Frigate uses 750 px² ≈ 27×27; SFace's input is 112×112, so smaller faces are mostly upscaled noise);
   - blur check with Laplacian variance;
   - rough frontalness from the 5 landmarks (eye–nose symmetry);
   - **skip grayscale/IR frames** (channel-variance test). Night faces mostly come back as "a person".
4. **Embed** with `alignCrop` + `feature` (128-d), then compute cosine similarity against the tenant's gallery. Each person keeps the maximum over their enrolled vectors, or a trimmed mean as in Frigate.
5. **Decide per clip, not per frame.** Name a person only if all three hold:
   - the best score is at least `FACES_THRESHOLD`;
   - it beats the runner-up person by a margin (for example 0.05);
   - it is seen in at least 2 frames, or in 1 frame with a very high score.

   Then discard every embedding (keep nothing for non-matches).
6. **Choose the threshold.** OpenCV's 0.363 is an LFW 1:1 *equal-error* threshold. 1:N matching on noisy footage needs it stricter. Start at about 0.45–0.50 and expose a slider with a "test on this clip" button. These values are my estimate (UNVERIFIED) and need calibration on real Blink clips.
7. **Pass the result to Claude as text:** `Recognized by FreeBlink's face matcher (not by you): "Mom".`
   - Update `SYSTEM_PROMPT`: "If a recognized name is given and a person in the clip plausibly matches, you may use that name; otherwise say 'someone'. Never infer identity yourself."
   - Add `people: string[]` to `OUTPUT_SCHEMA` so the UI and notifications get structured names.
8. **Event and notification.**
   - Add `"people": ["Mom"]` to the event JSON (names only, no vectors).
   - Notification title: `Front door: Mom arrived`.
   - Optionally use a lower ntfy priority for known household members, and per-person mute ("don't notify for Dad").
9. **YOLO mode.** Run faces only inside `person` boxes, as Frigate does, and add the names to the notification text.

### 6.4 Enrollment

- **From past clips (preferred).** The face then comes from the same camera, angle and lighting. In an event's detail view, "Who is this?" re-runs detection on demand on the cached clip (`BlinkService.clip_video`) and shows the face crops. The user picks one and names it. Only then is an embedding stored.
  - This avoids keeping an "unknown faces" inbox, which is Frigate's design and the legally risky part.
- **Upload photos.** Accept 1–5 clear frontal photos. Detect, align and embed them, and reject any photo without exactly one good face.
- **Gallery size.** Aim for 5–20 embeddings per person (Frigate recommends 20–30 images). Warn against near-duplicates.
- **Consent.**
  - Self-hosted: a checkbox per person ("I have this person's permission to recognise them").
  - Hosted, if ever enabled: an invite link the enrolled person opens to give their own electronic consent, with purpose and retention spelled out (BIPA 15(b), GDPR Art. 9(2)(a)).

### 6.5 Storage

- `data/users/<tid>/faces.enc` holds Fernet-encrypted JSON (`crypto.encrypt`, as for the Claude key), written atomically with mode 0600:

  ```json
  {"version": 1, "model": "sface-2021dec",
   "people": [{"id": "p_ab12", "name": "Mom", "consent": {"by": "owner-attested", "at": 1793...},
               "last_matched": 1793..., "embeddings": [{"v": "<base64 float32[128]>", "src": "clip:123", "at": 1793...}]}]}
  ```

- **Store the model id with the vectors.** Switching models means re-enrolling, because vectors from different models are not comparable.
- **Thumbnails.** One small face thumbnail per person for the UI. Encrypt it too, or skip it and show initials.
- **Deletion.**
  - Removing a person deletes their vectors immediately.
  - "Turn off face recognition" deletes the file.
  - `delete_account` already removes the folder.
  - A daily sweep removes people not matched for more than 12 months (retention policy).
- **Load cost.** One Fernet file per tenant, decrypted on each clip. The gallery is tiny (a few KB), so this is fine at FreeBlink's clip volume.

### 6.6 Unknown-person alerts

- Possible without storing anything: "a clear, frontal, colour face of sufficient size matched no one" becomes `Unrecognised person at Front door`.
- Expect false alarms for family members at night, from the side or at a distance. Ship it later, as an opt-in, and only with the quality gates above.
- **Do not** remember or cluster unknown faces ("the same stranger came 3 times"). That is the riskiest feature under EDPB guidance, BIPA and AI Act Art. 5(1)(e).

### 6.7 What the footage makes hard

- **Small faces.** Faces at the end of a driveway are a few dozen pixels, below what SFace handles well. YuNet's hard-set accuracy (0.75) shows it misses some small and occluded faces.
- **Angle.** Doorbells look down and cameras sit high, so many faces are partial profiles. SFace's CFP-FP of 94.8% and CPLFW of 91.05% are the relevant numbers, not LFW's 99.6%.
- **IR at night.** These models are trained on colour images (Frigate's warning). Expect few or no night matches. Frigate's advice is to add only a few night images, and only once colour recognition is stable.
- **Motion blur, backlight, hats, sunglasses and masks.** All reduce scores. Lean on the multi-frame vote and the margin rule.
- **Blink clips are short** (a few seconds). Sample at about 2–4 fps and skip to the frames with faces.
- **Mitigation:** always fall back to "a person". Never guess a name.

### 6.8 Footprint

| Item | Size |
|---|---|
| Models | about 39 MB on disk |
| `opencv-python-headless` + numpy | about 145 MB (arm64) / 216 MB (amd64) installed |
| Resident memory with models loaded | not measured (**UNVERIFIED**), likely around 100–250 MB |

- Probably fine on the 24 GB A1.
- On the 1 GB E2.1.Micro it should still fit next to uvicorn and ffmpeg. Measure before promising it.

## 7. Recommendation and integration plan

**Primary: YuNet + SFace through OpenCV, local, opt-in, self-host first.**
- Clean licence for any deployment.
- Small, runs on ARM and x86 with no compiler, and has a simple API.
- It uses the same detector Frigate chose.

**Fallback: InsightFace SCRFD + ArcFace (buffalo_s on E2.1.Micro, buffalo_l on A1) through onnxruntime, behind the same engine interface**, if SFace accuracy on real Blink footage disappoints.
- **Before shipping it in the image or on freeblink.app, get a commercial licence from InsightFace.**
- Cloud alternative: AWS Rekognition with the user's own AWS credentials. It makes consent and data transfer harder and costs about $0.001 per image.

**Rough plan for this codebase** (about 3–5 focused days, plus calibration on real clips):

| # | Work | Files | Size |
|---|---|---|---|
| 1 | Spike (per the Decision above, with InsightFace buffalo_l and buffalo_s; SFace alongside for comparison): run over 50–100 real Blink clips (day, night, doorbell, yard). Measure detection rate, face sizes, same-person and different-person scores, CPU time and RSS on A1 and E2.1.Micro. **Go/no-go gate.** | scratch script | 0.5–1 day |
| 2 | `FaceEngine`: lazy load, model download with hash check, detect, quality gates, embed, per-clip vote | new `app/faces.py` | ~200 lines |
| 3 | `FaceGallery`: Fernet-encrypted per-tenant storage, add/remove person, add embedding, retention sweep | new `app/face_gallery.py` (or extend `settings.py`) | ~120 lines |
| 4 | Config flags and the optional dependency (Docker build arg or `:faces` tag; README and env table) | `app/config.py`, `requirements.txt`, `Dockerfile`, `.github/workflows/deploy.yml`, `README.md` | small |
| 5 | Pipeline: run faces before Claude; pass `known_people` into the prompt; add `people` to the schema, event and notification title | `app/events.py`, `app/claude_vision.py` | ~60 lines |
| 6 | API: `GET/POST/DELETE /api/faces/people`, `POST /api/faces/people/{id}/photos`, `GET /api/events/{id}/faces` (on-demand crops), `POST /api/faces/enroll-from-event`, an opt-in toggle in settings | `app/main.py` | ~120 lines |
| 7 | UI: a "People" section in Settings (consent screen, list, add from photo, delete, threshold slider); "Who is this?" on event cards | `static/app.js`, `static/index.html`, `static/style.css` | ~250 lines |
| 8 | Privacy docs: README section, retention policy text, self-hoster responsibility note | `README.md` | small |
| 9 | Later: unknown-person alerts (opt-in), per-person mute and priority, optional InsightFace engine | — | — |

## 8. Open questions

1. **Hosted service.** Will face recognition ever run on freeblink.app, or stay self-host only? If hosted: where is the operator established, which Oracle region, and is a lawyer review possible? These decide GDPR controller/processor status, the need for an EU representative, and geofencing.
2. **Anthropic AUP interpretation.** Is it acceptable to pass names produced by a local face matcher to Claude as text? My reading: Claude does no identification. But the AUP bans products used "for facial recognition" in its surveillance section. Consider asking Anthropic, or ship the conservative variant (names added in code, not in the prompt).
3. **Accuracy on real Blink footage.** No published numbers exist for low-resolution doorbell or IR data. The spike in plan step 1 is the real decision point.
4. **SFace training data.** Which dataset the opencv_zoo ONNX was trained on is undocumented. Ask in opencv_zoo issues if provenance matters to you.
5. **InsightFace commercial licence.** Price and terms were not checked (contact recognition-oss-pack@insightface.ai).
6. **RAM and CPU on Oracle A1 and E2.1.Micro.** Not measured.
7. **Geofencing signal.** Blink accounts carry a region (US/EU) but not a state. Camera location for Illinois or Texas would need user self-declaration, which is weak evidence.
8. **AI Act classification** of a small private "known faces" gallery as remote biometric identification. No official guidance yet.

## Sources

**Code in this repo:** `app/events.py`, `app/detector.py`, `app/claude_vision.py`, `app/media.py`, `app/crypto.py`, `app/settings.py`, `app/tenants.py`, `app/config.py`, `app/blink_service.py`, `requirements.txt`, `Dockerfile`, `.github/workflows/deploy.yml`, `README.md`.

**Local models**
- OpenCV Zoo: https://github.com/opencv/opencv_zoo
  - YuNet: https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet
  - SFace: https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface
  - Benchmarks: https://github.com/opencv/opencv_zoo/blob/main/benchmark/README.md
- OpenCV face tutorial (accuracy and thresholds): https://github.com/opencv/opencv/blob/4.x/doc/tutorials/dnn/dnn_face/dnn_face.markdown
- opencv-python-headless: https://pypi.org/pypi/opencv-python-headless/json
- SFace paper and code: https://arxiv.org/abs/2205.12010, https://github.com/zhongyy/SFace
- InsightFace:
  - Licence: https://github.com/deepinsight/insightface/blob/master/README.md#license
  - Model zoo: https://github.com/deepinsight/insightface/blob/master/python-package/docs/model_zoo.md
  - SCRFD: https://github.com/deepinsight/insightface/blob/master/detection/scrfd/README.md
  - PyPI: https://pypi.org/pypi/insightface/json
- onnxruntime: https://pypi.org/pypi/onnxruntime/json
- dlib / face_recognition: https://pypi.org/pypi/dlib/json, https://github.com/davisking/dlib-models, https://pypi.org/pypi/face-recognition/json
- DeepFace: https://github.com/serengil/deepface
- MediaPipe BlazeFace model card: https://storage.googleapis.com/mediapipe-assets/MediaPipe%20BlazeFace%20Model%20Card%20(Short%20Range).pdf
- facenet-pytorch: https://github.com/timesler/facenet-pytorch
- CompreFace: https://github.com/exadel-inc/CompreFace
- Ultralytics: https://pypi.org/pypi/ultralytics/json, AGPL: https://www.gnu.org/licenses/agpl-3.0.html
- EdgeFace: https://github.com/otroshi/edgeface
- uniface licence table: https://yakhyo.github.io/uniface/license-attribution/

**Cloud and LLM**
- AWS Rekognition:
  - Collections: https://docs.aws.amazon.com/rekognition/latest/dg/collections.html
  - Pricing: https://aws.amazon.com/rekognition/pricing/
  - Service Terms §50: https://aws.amazon.com/service-terms/
  - Biometric terms: https://aws.amazon.com/legal/biometric-notice-and-consent-terms/
  - Availability changes: https://docs.aws.amazon.com/rekognition/latest/dg/rekognition-availability-changes.html
- Azure Face:
  - Limited Access: https://learn.microsoft.com/en-us/azure/foundry/responsible-ai/computer-vision/limited-access-identity
  - Microsoft Limited Access: https://learn.microsoft.com/en-us/azure/ai-services/cognitive-services-limited-access
  - Transparency note: https://learn.microsoft.com/en-us/azure/ai-foundry/responsible-ai/face/transparency-note
- Google Cloud Vision: https://docs.cloud.google.com/vision/docs/detecting-faces, https://docs.cloud.google.com/vision/docs/celebrity-recognition
- Anthropic: vision limitations https://platform.claude.com/docs/en/build-with-claude/vision; Usage Policy https://www.anthropic.com/legal/aup

**Ecosystem**
- Frigate:
  - Docs: https://docs.frigate.video/configuration/face_recognition
  - Releases: https://github.com/blakeblackshear/frigate/releases
  - Source: https://github.com/blakeblackshear/frigate/tree/v0.18.2/frigate
- Double Take: https://github.com/jakowenko/double-take
- Home Assistant PRs: https://github.com/home-assistant/core/pull/145767, https://github.com/home-assistant/core/pull/155450, https://github.com/home-assistant/core/pull/174977
- LLM Vision: https://github.com/valentinfrlch/ha-llmvision
- Vendors:
  - Google Nest: https://support.google.com/googlenest/answer/9268625
  - Ring: https://ring.com/support/articles/z3yhg/familiar-faces
  - Ring launch post: https://blog.ring.com/products-innovation/introducing-familiar-faces-a-new-way-to-see-whos-at-your-front-door/
  - Arlo: https://kb.arlo.com/000063157/
  - Wyze: https://www.wyze.com/pages/terms-of-service-friendly-faces

**Law**
- GDPR: https://eur-lex.europa.eu/eli/reg/2016/679/oj
- Ryneš C-212/13: https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:62013CJ0212
- EDPB Guidelines 3/2019: https://www.edpb.europa.eu/our-work-tools/our-documents/guidelines/guidelines-32019-processing-personal-data-through-video_en
- EDPB Guidelines 07/2020: https://www.edpb.europa.eu/our-work-tools/our-documents/guidelines/guidelines-072020-concepts-controller-and-processor-gdpr_en
- Dutch AP: https://www.autoriteitpersoonsgegevens.nl/en/themes/identification/biometrics/facial-recognition
- Irish DPC: https://www.dataprotection.ie/sites/default/files/uploads/2026-05/Domestic_CCTV_A5_ENG_Final.pdf
- AI Act: https://eur-lex.europa.eu/eli/reg/2024/1689/oj
  - Commission timeline: https://digital-strategy.ec.europa.eu/en/policies/regulatory-framework-ai
  - Council press release: https://www.consilium.europa.eu/en/press/press-releases/2026/06/29/artificial-intelligence-council-gives-final-green-light-to-simplify-and-streamline-rules/
- ICO:
  - Biometric guidance: https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/lawful-basis/biometric-data-guidance-biometric-recognition/how-do-we-process-biometric-data-lawfully/
  - Home CCTV: https://ico.org.uk/for-the-public/home-cctv-systems/
- BIPA:
  - Sec. 15: https://ftp.ilga.gov/ILCS/Ch%200740/Act%200014/074000140K15.html
  - P.A. 103-769: https://ftp.ilga.gov/Public%20Acts/103/103-0769.htm
  - Rosenbach: https://www.illinoiscourts.gov/Opinions/SupremeCourt/2019/123186.pdf
  - Zellmer v. Meta: https://cdn.ca9.uscourts.gov/datastore/opinions/2024/06/17/22-16925.pdf
- Texas CUBI: https://tcss.legis.texas.gov/resources/BC/htm/BC.503.htm
  - Texas AG, Meta: https://www.texasattorneygeneral.gov/news/releases/attorney-general-ken-paxton-secures-14-billion-settlement-meta-over-its-unauthorized-capture
  - Texas AG, Google: https://www.texasattorneygeneral.gov/news/releases/attorney-general-ken-paxton-finalizes-historic-settlement-google-and-secures-1375-billion-big-tech
- Washington: https://app.leg.wa.gov/rcw/default.aspx?cite=19.375, https://app.leg.wa.gov/rcw/default.aspx?cite=19.373
- Colorado HB24-1130: https://leg.colorado.gov/bills/hb24-1130
- California: https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=CIV&sectionNum=1798.140
- Portland: https://www.portland.gov/code/34/10
