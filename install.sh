#!/bin/bash
# flow-to-api installer (Ubuntu 22.04 / 24.04).
# Installs the Flow bridge service + the one-time noVNC login helper.
#
# Usage:
#   git clone https://github.com/newabolup/flow-to-api.git
#   cd flow-to-api
#   bash install.sh
#
# After install: run ~/flowbridge/login.sh, log in to Google in the
# printed link, then run ~/flowbridge/stop-login.sh.
set -e
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$HOME/flowbridge"
USER_NAME="$(whoami)"

echo "==> Installing system packages (needs sudo)..."
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip xvfb x11vnc websockify git curl

echo "==> Preparing $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
cp "$REPO_DIR/bridge.py" "$REPO_DIR/login.sh" "$REPO_DIR/stop-login.sh" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/login.sh" "$INSTALL_DIR/stop-login.sh"

if [ ! -d "$INSTALL_DIR/venv" ]; then
  python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install fastapi uvicorn httpx python-multipart playwright

echo "==> Installing Playwright Chromium + OS dependencies..."
"$INSTALL_DIR/venv/bin/playwright" install chromium
sudo "$INSTALL_DIR/venv/bin/playwright" install-deps chromium || echo "!! install-deps reported warnings; continuing (headed Chromium may miss some libs)"

if [ ! -x "$INSTALL_DIR/cloudflared" ]; then
  ARCH="$(uname -m)"
  case "$ARCH" in
    x86_64) CF_ARCH=amd64 ;;
    aarch64) CF_ARCH=arm64 ;;
    *) echo "!! unsupported arch: $ARCH (cloudflared skipped; the remote login link will not work)"; CF_ARCH="" ;;
  esac
  if [ -n "$CF_ARCH" ]; then
    echo "==> Downloading cloudflared ($CF_ARCH)..."
    curl -L "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$CF_ARCH" -o "$INSTALL_DIR/cloudflared"
    chmod +x "$INSTALL_DIR/cloudflared"
  fi
fi

if [ ! -d "$INSTALL_DIR/novnc" ]; then
  echo "==> Cloning noVNC..."
  git clone https://github.com/novnc/noVNC.git "$INSTALL_DIR/novnc"
fi

echo "==> Installing systemd units..."
sed -e "s|__USER__|$USER_NAME|g" -e "s|__HOME__|$HOME|g" "$REPO_DIR/flowbridge-xvfb.service" | sudo tee /etc/systemd/system/flowbridge-xvfb.service >/dev/null
sed -e "s|__USER__|$USER_NAME|g" -e "s|__HOME__|$HOME|g" "$REPO_DIR/flowbridge.service" | sudo tee /etc/systemd/system/flowbridge.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable flowbridge-xvfb.service flowbridge.service
sudo systemctl restart flowbridge-xvfb.service
sudo systemctl restart flowbridge.service

echo
echo "Done. Next steps:"
echo "  1. Log in to Google Flow in the server browser:  $INSTALL_DIR/login.sh"
echo "     (open the printed link, sign in, then run $INSTALL_DIR/stop-login.sh)"
echo "  2. Check health:  curl http://127.0.0.1:8123/health"
echo "  3. Logs:  sudo journalctl -u flowbridge -f"
