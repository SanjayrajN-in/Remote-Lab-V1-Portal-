#!/usr/bin/env bash
# Install the Remote Lab portal on a Debian-based server.
#
#   sudo ./install/install.sh
#
# Idempotent: safe to re-run to pick up code changes. It never overwrites an
# existing .env, so re-running cannot sever a working configuration.

set -euo pipefail

APP_NAME="remote-lab-portal"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
RUN_USER="${SUDO_USER:-$(whoami)}"
PORT="${PORT:-5000}"

say()  { printf '\n\033[1;36m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m !\033[0m %s\n' "$1"; }
die()  { printf '\033[1;31m ✗\033[0m %s\n' "$1" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run this with sudo."

if ! command -v apt-get >/dev/null 2>&1; then
  die "This installer expects apt. On other distributions, follow the manual steps in DEPLOY.md."
fi

say "Installing system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip

say "Creating the virtual environment"
cd "$APP_DIR"
if [ ! -d venv ]; then
  python3 -m venv venv
fi
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -r requirements.txt

say "Preparing directories"
mkdir -p data uploads static/sop
chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR"

if [ ! -f .env ]; then
  say "Generating .env"
  cp .env.example .env
  SECRET=$(./venv/bin/python -c 'import secrets; print(secrets.token_hex(32))')
  NODE_SECRET=$(./venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')
  IP=$(hostname -I | awk '{print $1}')
  sed -i "s|^SECRET_KEY=.*|SECRET_KEY=${SECRET}|" .env
  sed -i "s|^NODE_SHARED_SECRET=.*|NODE_SHARED_SECRET=${NODE_SECRET}|" .env
  sed -i "s|^PORTAL_BASE_URL=.*|PORTAL_BASE_URL=http://${IP}:${PORT}|" .env
  chown "$RUN_USER":"$RUN_USER" .env
  chmod 600 .env
  echo
  echo "  NODE_SHARED_SECRET = ${NODE_SECRET}"
  echo "  Put that same value in every Lab Pi's .env."
else
  warn ".env already exists — left untouched."
fi

say "Initialising the database"
sudo -u "$RUN_USER" ./venv/bin/python seed.py || warn "Seed reported a problem; check the output above."

say "Installing the systemd service"
sed -e "s|__APP_DIR__|${APP_DIR}|g" \
    -e "s|__USER__|${RUN_USER}|g" \
    -e "s|__PORT__|${PORT}|g" \
    install/remote-lab-portal.service > "$SERVICE_FILE"

systemctl daemon-reload
systemctl enable --quiet "$APP_NAME"
systemctl restart "$APP_NAME"
sleep 2

if systemctl is-active --quiet "$APP_NAME"; then
  IP=$(hostname -I | awk '{print $1}')
  say "Done"
  echo "  Portal:  http://${IP}:${PORT}"
  echo "  Logs:    journalctl -u ${APP_NAME} -f"
  echo "  Restart: systemctl restart ${APP_NAME}"
else
  echo
  die "The service did not start. Run: journalctl -u ${APP_NAME} -n 50 --no-pager"
fi
