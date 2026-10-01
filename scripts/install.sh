#!/usr/bin/env bash
# VPS par one-time setup:  sudo bash scripts/install.sh
set -euo pipefail
APP_DIR=/opt/ats-structurer

command -v python3 >/dev/null && python3 -c "import venv, ensurepip" 2>/dev/null || \
    { apt-get update -q && apt-get install -y -q python3 python3-venv python3-pip; }

mkdir -p "$APP_DIR"
cp -r . "$APP_DIR"/
cd "$APP_DIR"

python3 -m venv venv
./venv/bin/pip install --upgrade pip -q
./venv/bin/pip install -r requirements.txt -q

[ -f .env ] || { cp .env.example .env; echo ">> .env bana diya, ab usme keys/URLs bharo: nano $APP_DIR/.env"; }
chmod 600 .env
mkdir -p data logs
chown -R www-data:www-data "$APP_DIR"

cp deploy/structurer.service /etc/systemd/system/structurer.service
systemctl daemon-reload
systemctl enable structurer
echo ">> Ab .env edit karke chalao:  sudo systemctl restart structurer && journalctl -u structurer -f"
