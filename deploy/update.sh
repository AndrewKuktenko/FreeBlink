#!/usr/bin/env bash
# Auto-update FreeBlink. setup.sh runs this every 5 minutes from cron (/etc/cron.d/freeblink).
# It pulls the latest code (compose file, scripts) and the latest image built by GitHub Actions,
# and restarts only the containers whose image changed. Data in the freeblink-data volume is kept.
set -euo pipefail
cd "$(dirname "$0")/.."

exec 9>/tmp/freeblink-update.lock
flock -n 9 || exit 0  # a previous run is still going

SUDO=""; [ "$(id -u)" = 0 ] || SUDO=sudo
log() { echo "$(date -Is) $*"; }

git fetch -q origin main
if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
  git reset -q --hard origin/main  # .env and data are untracked, so they're kept
  log "code updated to $(git rev-parse --short HEAD)"
fi

ids() { $SUDO docker compose images -q 2>/dev/null | sort | tr '\n' ' '; }
before=$(ids)
if ! out=$($SUDO docker compose pull -q 2>&1); then log "pull failed: $out"; exit 1; fi
$SUDO docker compose up -d --no-build --remove-orphans --quiet-pull 2>&1 | grep -v "Running\|Up-to-date" || true
after=$(ids)

if [ "$before" != "$after" ]; then
  log "restarted with new images"
  $SUDO docker image prune -f >/dev/null
fi
