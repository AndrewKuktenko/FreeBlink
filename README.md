# FreeBlink

A self-hosted web dashboard for your Blink cameras, doorbell and Sync Module, with AI descriptions of every motion clip pushed to your phone:

- See every system (Sync Module, standalone Minis, doorbells) and its cameras
- Arm and disarm systems; turn motion detection on and off per camera
- Take a new photo and see the latest thumbnails, battery, temperature and Wi-Fi
- **Live view** in the browser
- Browse and play recent clips (cloud and Sync Module USB), each with an AI description
- Push a short AI description of every motion clip to your phone

Built on [blinkpy](https://github.com/fronzbot/blinkpy), the reverse-engineered library Home Assistant uses. Blink has **no official API**, so things can break when Blink changes its servers. Upgrading blinkpy (`.venv/bin/pip install -U blinkpy`) is usually the fix.

## Run it

Requirements: Python 3.9+ and `ffmpeg` (`brew install ffmpeg`; needed for live view only).

```bash
cp .env.example .env    # optional; defaults run on http://127.0.0.1:8000
./run.sh
```

Open http://127.0.0.1:8000 and sign in with your Blink account. Blink then sends a verification code to your email or phone. Enter it, and you're in.

After the first sign-in the app keeps Blink's OAuth refresh token in `data/blink_credentials.json` (mode 600). It does **not** keep your password, so restarts don't need a new code. If Blink revokes the token, you'll be asked to sign in again. *Sign out* deletes the file.

## Using it from your phone or another computer

Set these in `.env`:

```
HOST=0.0.0.0
APP_PASSWORD=pick-something-long
```

The app won't start on a non-localhost address without `APP_PASSWORD`, because anyone who can reach it can watch your cameras. It uses HTTP Basic auth, so keep it on your home network. For access away from home, put it behind a VPN such as Tailscale rather than opening a port on your router.

## How live view works

```
Blink cloud ──immis (TLS)──> blinkpy BlinkLiveStream ──MPEG-TS on 127.0.0.1:random──> ffmpeg ──HLS──> data/live/<camera>/ ──> browser (hls.js)
```

- Expect 10–20 s to connect and a few seconds of delay. Battery cameras have to wake up first.
- Blink ends live sessions after its own time limit, which depends on your plan and camera. The player then shows "Stream ended". Press *Live view* again to restart.
- A stream stops automatically about 20 s after the last viewer closes it (`LIVE_IDLE_TIMEOUT`), because live view drains camera batteries quickly.

## AI clip descriptions and phone notifications

Every 30 s the app looks for new motion clips, in Blink's cloud and on the Sync Module's USB drive. For **every** new clip it sends 4 frames (640 px wide) to **Claude Sonnet 5.5** (effort `low`). Claude returns a short headline and a 1–2 sentence description (at most 30 words) of what happens. The description appears under the clip in *Clips* and is pushed to your phone with the best frame attached.

- **Key:** paste it in *Settings → Clip descriptions* (or set `ANTHROPIC_API_KEY` in `.env`). Create one at https://platform.claude.com/settings/keys. It must be an API key; a Claude.ai subscription doesn't cover API calls.
- **Phone:** install the **ntfy** app (iOS / Android), subscribe to the topic shown in *Settings → Phone notifications*, and allow ntfy notifications in your phone's settings. Treat the topic like a password.
- **Tokens:** about 2,000 input and 75 output tokens per clip (measured on 15 real clips). `AI_FRAMES` and `AI_FRAME_WIDTH` trade detail for cost; below 640 px, people far from the camera get missed.
- **Refusals:** server-side refusal fallback (`fallbacks: "default"`) is on. If a safety classifier declines a request, it's retried on another model.
- **Startup:** clips recorded before the app started are never described, so you won't get a flood of old notifications.
- **Free alternative:** `DETECT_ENGINE=yolo` runs a local YOLO model instead (`pip install ultralytics`). It only labels objects; it doesn't describe them.

## Blink Mini Pan-Tilt Mount

During live view on a Mini with the mount, a D-pad appears (arrow keys work too), plus *Home*, *Set as home* and *360° overview*. The mount has no REST API. Commands go over the live-view socket, using the protocol recovered by [blink-camera-mcp](https://github.com/AbhijatSaxena/blink-camera-mcp) (MIT), see `app/pantilt.py`. The D-pad shows up only after the mount reports its position.

## Layout

| Path | What it does |
| --- | --- |
| `app/blink_service.py` | Blink session: login/2FA, token persistence, polling, arm, snap, thumbnails, clips |
| `app/live.py` | Live view: BlinkLiveStream → ffmpeg → HLS, with idle shutdown |
| `app/main.py` | FastAPI routes and Basic auth |
| `static/` | Dashboard UI (plain HTML/CSS/JS) |

## Not covered yet

- Two-way talk on the doorbell. blinkpy doesn't implement it.

## Disclaimer

FreeBlink is an unofficial personal project. It is not affiliated with, endorsed by or supported by Blink or Amazon. It uses Blink's private, undocumented API, so use it at your own risk.
