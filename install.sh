#!/bin/bash
# flow-to-api installer (Ubuntu 22.04 / 24.04).
#
#   git clone https://github.com/newabolup/flow-to-api.git
#   cd flow-to-api && bash install.sh
#
# Options:
#   --with-api-key   generate a random API key, require it on every endpoint
#                    (recommended if anything other than localhost talks to it)
#   --skip-login-tools   do not install cloudflared/noVNC (if you log in by
#                        another route, e.g. copying an existing profile)
#   --no-start       install and enable, but do not start the services now
#
# Safe to re-run: system packages, the venv and the profile are reused.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="${FLOWBRIDGE_HOME:-$HOME/flowbridge}"
USER_NAME="$(whoami)"
WITH_API_KEY=0
LOGIN_TOOLS=1
START=1

for arg in "$@"; do
  case "$arg" in
    --with-api-key) WITH_API_KEY=1 ;;
    --skip-login-tools) LOGIN_TOOLS=0 ;;
    --no-start) START=0 ;;
    *) echo "!! unknown option: $arg"; exit 2 ;;
  esac
done

export DEBIAN_FRONTEND=noninteractive

echo "==> Installing system packages (needs sudo)..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip xvfb x11vnc \
  websockify git curl ca-certificates

echo "==> Preparing $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR" "$INSTALL_DIR/debug" "$INSTALL_DIR/tmp"
# The application code (bridge.py is a thin shim over the flowbridge package).
cp -r "$REPO_DIR/flowbridge" "$INSTALL_DIR/"
cp "$REPO_DIR/bridge.py" "$REPO_DIR/requirements.txt" "$INSTALL_DIR/"
cp -r "$REPO_DIR/tools" "$INSTALL_DIR/"
cp "$REPO_DIR/login.sh" "$REPO_DIR/stop-login.sh" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/login.sh" "$INSTALL_DIR/stop-login.sh"
find "$INSTALL_DIR/flowbridge" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
# The login scripts use the installed location, not the repo checkout.
sed -i "s|^BASE=.*|BASE=\"$INSTALL_DIR\"|" "$INSTALL_DIR/login.sh" "$INSTALL_DIR/stop-login.sh"

if [ ! -d "$INSTALL_DIR/venv" ]; then
  python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
echo "==> Installing Python dependencies (pinned in requirements.txt)..."
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

echo "==> Installing Playwright Chromium + OS dependencies..."
"$INSTALL_DIR/venv/bin/playwright" install chromium
sudo "$INSTALL_DIR/venv/bin/playwright" install-deps chromium \
  || echo "!! install-deps reported warnings; continuing (headed Chromium may miss some libs)"

if [ "$LOGIN_TOOLS" = "1" ]; then
  if [ ! -x "$INSTALL_DIR/cloudflared" ]; then
    ARCH="$(uname -m)"
    case "$ARCH" in
      x86_64) CF_ARCH=amd64 ;;
      aarch64|arm64) CF_ARCH=arm64 ;;
      *) echo "!! unsupported arch: $ARCH (cloudflared skipped)"; CF_ARCH="" ;;
    esac
    if [ -n "$CF_ARCH" ]; then
      curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$CF_ARCH" \
        -o "$INSTALL_DIR/cloudflared"
      chmod +x "$INSTALL_DIR/cloudflared"
    fi
  fi
  if [ ! -d "$INSTALL_DIR/novnc" ]; then
    git clone --depth 1 https://github.com/novnc/noVNC.git "$INSTALL_DIR/novnc"
    rm -rf "$INSTALL_DIR/novnc/.git"
  fi
fi

echo "==> Installing systemd units..."
ENV_FILE=/etc/flowbridge.env
if [ "$WITH_API_KEY" = "1" ] && [ ! -f "$ENV_FILE" ]; then
  API_KEY="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)"
  sudo tee "$ENV_FILE" >/dev/null <<ENVEOF
# flowbridge environment overrides (read by the unit)
FLOWBRIDGE_API_KEY=$API_KEY
ENVEOF
  sudo chmod 600 "$ENV_FILE"
  echo "    API key written to $ENV_FILE"
fi

sed -e "s|__USER__|$USER_NAME|g" -e "s|__HOME__|$HOME|g" -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
  "$REPO_DIR/flowbridge-xvfb.service" | sudo tee /etc/systemd/system/flowbridge-xvfb.service >/dev/null
sed -e "s|__USER__|$USER_NAME|g" -e "s|__HOME__|$HOME|g" -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
  "$REPO_DIR/flowbridge.service" | sudo tee /etc/systemd/system/flowbridge.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable flowbridge-xvfb.service flowbridge.service >/dev/null
if [ "$START" = "1" ]; then
  sudo systemctl restart flowbridge-xvfb.service
  sudo systemctl restart flowbridge.service
fi

echo
echo "Done."
echo "  1. Log in to Google Flow once:   $INSTALL_DIR/login.sh"
echo "     (open the printed link, sign in, then run $INSTALL_DIR/stop-login.sh)"
echo "  2. Check the UI layer, no credits spent:"
echo "       $INSTALL_DIR/venv/bin/python $INSTALL_DIR/tools/check_ui.py --models"
echo "  3. Health:  curl -s localhost:8123/health | python3 -m json.tool"
echo "  4. Logs:    sudo journalctl -u flowbridge -f"
if [ "$WITH_API_KEY" = "1" ]; then
  echo "  API key:    sudo cat $ENV_FILE   (send it as X-API-Key)"
fi
