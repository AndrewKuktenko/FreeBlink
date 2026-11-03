#!/usr/bin/env bash
# One-time setup of FreeBlink on a fresh Ubuntu 22.04/24.04 server (e.g. Oracle Cloud Always Free).
#
#   Cloudflare Tunnel (recommended, no open ports):
#     curl -fsSL https://raw.githubusercontent.com/AndrewKuktenko/FreeBlink/main/deploy/setup.sh | bash -s -- <TUNNEL_TOKEN>
#   Caddy + Let's Encrypt (needs ports 80/443 open and the domain pointing at this server):
#     curl -fsSL https://raw.githubusercontent.com/AndrewKuktenko/FreeBlink/main/deploy/setup.sh | bash -s -- your.domain
#   Re-running it without an argument keeps the existing ~/FreeBlink/.env.
#
# It also installs a cron job that keeps the server on the latest version (deploy/update.sh, every 5 minutes).
set -euo pipefail

ARG="${1:-}"
if [ -z "$ARG" ] && ! grep -qE "^(TUNNEL_TOKEN|DOMAIN)=.+" "$HOME/FreeBlink/.env" 2>/dev/null; then
  read -rp "Cloudflare tunnel token (or a domain for Caddy): " ARG < /dev/tty
  [ -n "$ARG" ] || { echo "Give a tunnel token or a domain."; exit 1; }
fi

# Tunnel tokens are long base64 strings (start with eyJ); anything else is treated as a domain.
if [ -z "$ARG" ]; then MODE=keep
elif [[ "$ARG" == eyJ* && ${#ARG} -gt 100 ]]; then MODE=tunnel
else MODE=caddy; fi

set_env() {  # set_env KEY VALUE  -> writes/updates KEY=VALUE in .env
  if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi
}

echo "==> Installing Docker"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
fi

if [ "$MODE" = caddy ]; then
  echo "==> Opening ports 80 and 443 in the server firewall"
  # Oracle's Ubuntu images block everything but SSH with iptables, even if the cloud firewall allows it.
  if sudo iptables -L INPUT -n | grep -q "REJECT"; then
    for port in 80 443; do
      sudo iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null \
        || sudo iptables -I INPUT 5 -p tcp --dport "$port" -j ACCEPT
    done
    sudo iptables -C INPUT -p udp --dport 443 -j ACCEPT 2>/dev/null \
      || sudo iptables -I INPUT 5 -p udp --dport 443 -j ACCEPT
    if command -v netfilter-persistent >/dev/null; then sudo netfilter-persistent save; fi
  fi
  if command -v ufw >/dev/null && sudo ufw status | grep -q "Status: active"; then
    sudo ufw allow 80/tcp && sudo ufw allow 443
  fi
fi

# Small servers (e.g. Oracle's 1 GB micro) need swap for ffmpeg and image updates.
MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if [ "$MEM_MB" -lt 2000 ] && ! swapon --show | grep -q .; then
  echo "==> Adding 2 GB of swap"
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
  grep -q "^/swapfile" /etc/fstab || echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab >/dev/null
fi

echo "==> Getting FreeBlink"
if [ ! -d "$HOME/FreeBlink" ]; then
  git clone https://github.com/AndrewKuktenko/FreeBlink.git "$HOME/FreeBlink"
fi
cd "$HOME/FreeBlink"
git pull --ff-only
[ -f .env ] || cp .env.example .env
chmod 600 .env

if [ "$MODE" = tunnel ]; then
  set_env COMPOSE_PROFILES tunnel
  set_env TUNNEL_TOKEN "$ARG"
elif [ "$MODE" = caddy ]; then
  set_env COMPOSE_PROFILES caddy
  set_env DOMAIN "$ARG"
elif grep -q "^TUNNEL_TOKEN=." .env; then MODE=tunnel
else MODE=caddy; fi

echo "==> Starting FreeBlink"
if sudo docker compose pull -q; then
  sudo docker compose up -d --no-build --remove-orphans
else
  echo "Couldn't download the published image; building it here instead (takes a few minutes)."
  sudo docker compose up -d --build --remove-orphans
fi

echo "==> Turning on automatic updates (every 5 minutes)"
chmod +x "$PWD/deploy/update.sh"
sudo touch /var/log/freeblink-update.log && sudo chown "$(id -un)" /var/log/freeblink-update.log
echo "*/5 * * * * $(id -un) $PWD/deploy/update.sh >> /var/log/freeblink-update.log 2>&1" \
  | sudo tee /etc/cron.d/freeblink >/dev/null
sleep 5
sudo docker compose ps

if [ "$MODE" = tunnel ]; then
  cat <<MSG

Done. FreeBlink is running behind your Cloudflare Tunnel.
Open the public hostname you set in Cloudflare (e.g. https://freeblink.yourdomain.com).
In Cloudflare Zero Trust > Networks > Tunnels, the tunnel should show as HEALTHY.

Logs:    cd ~/FreeBlink && sudo docker compose logs -f
Updates: automatic, every 5 minutes (log: /var/log/freeblink-update.log)
MSG
else
  IP=$(curl -fsS https://api.ipify.org || echo "this server's public IP")
  cat <<MSG

Done. FreeBlink is starting at https://$ARG

If the page doesn't load:
  - Point $ARG to $IP.
  - In Oracle Cloud, add ingress rules for TCP 80 and 443 from 0.0.0.0/0 to the VCN security list.

Logs:    cd ~/FreeBlink && sudo docker compose logs -f
Updates: automatic, every 5 minutes (log: /var/log/freeblink-update.log)
MSG
fi
