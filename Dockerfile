FROM python:3.12-slim

# ffmpeg: live view (HLS) and clip frames for AI descriptions
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app app
COPY static static

# Run unprivileged; all state (tokens, clips, settings) lives in the /data volume.
RUN useradd --create-home --uid 1000 freeblink && mkdir /data && chown freeblink /data
USER freeblink
ENV HOST=0.0.0.0 PORT=8000 BLINK_DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000

# Caddy terminates HTTPS in front; trust its X-Forwarded-* headers (the app port is never published).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
