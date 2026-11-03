#!/usr/bin/env bash
# One-time setup of FreeBlink on a fresh Ubuntu 22.04/24.04 server (e.g. Oracle Cloud Always Free).
# Usage:  curl -fsSL https://raw.githubusercontent.com/AndrewKuktenko/FreeBlink/main/deploy/setup.sh | bash -s -- your.domain
set -euo pipefail

DOMAIN="${1:-}"
if [ -z "$DOMAIN" ]; then
  read -rp "Domain for FreeBlink (e.g. freeblink.duckdns.org): " DOMAIN < /dev/tty
fi
[ -n "$DOMAIN" ] || { echo "A domain is required."; exit 1; }

echo "==> Installing Docker"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
fi

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

echo "==> Getting FreeBlink"
if [ ! -d "$HOME/FreeBlink" ]; then
  git clone https://github.com/AndrewKuktenko/FreeBlink.git "$HOME/FreeBlink"
fi
cd "$HOME/FreeBlink"
git pull --ff-only

if [ ! -f .env ]; then
  cp .env.example .env
fi
grep -q '^DOMAIN=' .env && sed -i "s|^DOMAIN=.*|DOMAIN=$DOMAIN|" .env || echo "DOMAIN=$DOMAIN" >> .env

echo "==> Starting FreeBlink (first build takes a few minutes)"
sudo docker compose up -d --build

IP=$(curl -fsS https://api.ipify.org || echo "this server's public IP")
cat <<MSG

Done. FreeBlink is starting at https://$DOMAIN

If the page doesn't load:
  - Point $DOMAIN to $IP (DuckDNS: put $IP in the "current ip" box).
  - In Oracle Cloud: Networking > Virtual cloud networks > your VCN > Security Lists >
    add Ingress rules for TCP 80 and 443 from 0.0.0.0/0.
  - Logs: cd ~/FreeBlink && sudo docker compose logs -f

Update later with: cd ~/FreeBlink && git pull && sudo docker compose up -d --build
MSG
