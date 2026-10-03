#!/usr/bin/env bash
# Install GeminiBot as an always-on systemd service on Ubuntu/Debian.
# Run from the repo folder:  bash deploy/install.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_USER="$(whoami)"
SERVICE=geminibot

echo "==> Installing system packages"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git

echo "==> Setting up Python environment"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  echo
  echo "!! Created $APP_DIR/.env — add your keys with:  nano $APP_DIR/.env"
  echo "!! Then run this script again."
  exit 1
fi

echo "==> Installing systemd service"
sudo tee /etc/systemd/system/$SERVICE.service > /dev/null <<EOF
[Unit]
Description=GeminiBot Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now $SERVICE
sudo systemctl restart $SERVICE
sleep 3
sudo systemctl --no-pager status $SERVICE | head -5
echo
echo "Done. Logs:  journalctl -u $SERVICE -f"
