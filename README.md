# FreeBlink

A self-hosted web dashboard for your Blink cameras, doorbell and Sync Module, with AI descriptions of every motion clip pushed to your phone:

- See every system (Sync Module, standalone Minis, doorbells) and its cameras
- Arm and disarm systems; turn motion detection on and off per camera
- Take a new photo and see the latest thumbnails, battery, temperature and Wi-Fi
- **Live view** in the browser
- Browse and play recent clips (cloud and Sync Module USB), each with an AI description
- Push a short AI description of every motion clip to your phone

Built on [blinkpy](https://github.com/fronzbot/blinkpy), the reverse-engineered library Home Assistant uses. Blink has **no official API**, so things can break when Blink changes its servers. Upgrading blinkpy (`.venv/bin/pip install -U blinkpy`) is usually the fix.

## Accounts

Anyone signs in with their **own Blink account** (email, password and Blink's verification code). There are no separate FreeBlink accounts. Each Blink account gets its own isolated space on the server: cameras, clips, AI descriptions, notification topic and Claude key. One account can never see another's cameras.

- **Passwords** go to Blink once and are never stored. Blink's sign-in token is stored **encrypted** (Fernet, key in `data/secret.key` or `SECRET_KEY`).
- **Sign out on this device** ends the browser session. The account keeps being watched, so phone notifications continue.
- **Delete my FreeBlink data** (Settings) stops watching, forgets the Blink token and erases that account's clips, descriptions and settings.
- Sign-in attempts are rate-limited per IP, and live views are limited per account (`MAX_LIVE_PER_USER`) and per server (`MAX_LIVE_TOTAL`).

## Deploy to a server (free)

FreeBlink needs a server that runs all the time (it watches for clips and runs ffmpeg), so serverless hosts like Cloudflare Workers won't work. The recommended setup costs only the domain:

```
users ─https─> freeblink.yourdomain.com ─> Cloudflare (HTTPS) ─tunnel─> Oracle Cloud Always Free server ─> FreeBlink (Docker)
```

1. **Domain:** buy one in the Cloudflare dashboard (*Domain Registration → Register Domains*).
2. **Tunnel:** *Zero Trust → Networks → Tunnels → Create a tunnel → Cloudflared*. Copy the token from the install command, then add a public hostname `freeblink.yourdomain.com` with service `HTTP` → `app:8000`.
3. **Server:** in Oracle Cloud (free account, upgraded to *Pay As You Go* so idle instances aren't reclaimed), create an *Ubuntu 24.04* instance with shape *VM.Standard.A1.Flex* (2 OCPU, 12 GB).
4. **Install:** SSH in and run
   ```bash
   curl -fsSL https://raw.githubusercontent.com/AndrewKuktenko/FreeBlink/main/deploy/setup.sh | bash -s -- <TUNNEL_TOKEN>
   ```

No ports need to be opened; the server only makes outgoing connections. Without Cloudflare, pass a domain instead of the token (`... | bash -s -- freeblink.duckdns.org`). That uses Caddy with Let's Encrypt and needs ports 80/443 open.

Update: `cd ~/FreeBlink && git pull && sudo docker compose up -d --build`. Logs: `sudo docker compose logs -f`. All data lives in the `freeblink-data` Docker volume; back it up together with `secret.key` inside it.

## Run it locally

Requirements: Python 3.10+ and `ffmpeg` (`brew install ffmpeg`).

```bash
cp .env.example .env    # optional; defaults run on http://127.0.0.1:8000
./run.sh
```

Or with Docker: set `DOMAIN=localhost` in `.env` and run `docker compose up -d --build`.

## How live view works

```
Blink cloud ──immis (TLS)──> blinkpy BlinkLiveStream ──MPEG-TS on 127.0.0.1:random──> ffmpeg ──HLS──> data/live/<camera>/ ──> browser (hls.js)
```

- Expect 10–20 s to connect and a few seconds of delay. Battery cameras have to wake up first.
- Blink ends live sessions after its own time limit, which depends on your plan and camera. The player then shows "Stream ended". Press *Live view* again to restart.
- A stream stops automatically about 20 s after the last viewer closes it (`LIVE_IDLE_TIMEOUT`), because live view drains camera batteries quickly.

## AI clip descriptions and phone notifications

Every 30 s the app looks for new motion clips, in Blink's cloud and on the Sync Module's USB drive. For **every** new clip it sends 4 frames (640 px wide) to **Claude Sonnet 5.5** (effort `low`). Claude returns a short headline and a 1–2 sentence description (at most 30 words) of what happens. The description appears under the clip in *Clips* and is pushed to your phone with the best frame attached.

- **Key:** each user pastes their own key in *Settings → Clip descriptions*; usage is billed to their Anthropic account. Create one at https://platform.claude.com/settings/keys (a Claude.ai subscription doesn't cover API calls). Without a key, users still get a plain "Motion" notification per clip.
- **Phone:** install the **ntfy** app (iOS / Android), subscribe to the random topic FreeBlink created for the account (*Settings → Phone notifications*), and allow ntfy notifications in your phone's settings. Treat the topic like a password.
- **Tokens:** about 2,000 input and 75 output tokens per clip (measured on 15 real clips). `AI_FRAMES` and `AI_FRAME_WIDTH` trade detail for cost; below 640 px, people far from the camera get missed.
- **Refusals:** server-side refusal fallback (`fallbacks: "default"`) is on. If a safety classifier declines a request, it's retried on another model.
- **Startup:** clips recorded before the app started are never described, so you won't get a flood of old notifications.
- **Free alternative:** `DETECT_ENGINE=yolo` runs a local YOLO model instead (`pip install ultralytics`). It only labels objects; it doesn't describe them.

## Blink Mini Pan-Tilt Mount

During live view on a Mini with the mount, a D-pad appears (arrow keys work too), plus *Home*, *Set as home* and *360° overview*. The mount has no REST API. Commands go over the live-view socket, using the protocol recovered by [blink-camera-mcp](https://github.com/AbhijatSaxena/blink-camera-mcp) (MIT), see `app/pantilt.py`. The D-pad shows up only after the mount reports its position.

## Layout

| Path | What it does |
| --- | --- |
| `app/tenants.py` | Accounts: per-Blink-account isolation, sessions, 2FA in progress, rate limits, deletion |
| `app/blink_service.py` | One Blink session: login/2FA, encrypted token, polling, arm, snap, thumbnails, clips |
| `app/live.py` | Live view: BlinkLiveStream → ffmpeg → HLS, with idle shutdown |
| `app/main.py` | FastAPI routes, session cookies, cross-site request protection |
| `Dockerfile`, `docker-compose.yml`, `deploy/` | Container, Caddy HTTPS, server setup script |
| `static/` | Dashboard UI (plain HTML/CSS/JS) |

## Not covered yet

- Two-way talk on the doorbell. blinkpy doesn't implement it.

## Disclaimer

FreeBlink is an unofficial personal project. It is not affiliated with, endorsed by or supported by Blink or Amazon. It uses Blink's private, undocumented API, so use it at your own risk.

If you run a FreeBlink server for other people, you are holding their Blink sign-in tokens and home video. Keep the server updated, keep backups of the data volume private, and tell your users how to delete their data (Settings → Delete my FreeBlink data). Automated access to Blink accounts may conflict with Blink's terms of service.
