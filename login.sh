#!/bin/bash
# One-time Google login for the Flow bridge (flow-to-api), via a temporary noVNC page.
# The owner opens the printed cloudflared URL on their own device, logs into
# Google in the server's Chromium (same profile the bridge service uses),
# then tells the assistant, which stops this stack (browser kept logged in
# via the profile dir). Xvfb is left running for the bridge service.
set -u
BASE=~/flowbridge
CHROME=$(ls -d ~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome 2>/dev/null | head -1)
[ -n "$CHROME" ] || { echo "chromium binary not found"; exit 1; }
export DISPLAY=:99
pkill -f "Xvfb :99" 2>/dev/null; sleep 1
Xvfb :99 -screen 0 1280x900x24 >/tmp/xvfb.log 2>&1 &
sleep 1
x11vnc -display :99 -forever -shared -rfbport 5900 -nopw -listen localhost >/tmp/x11vnc.log 2>&1 &
websockify --web "$BASE/novnc" 6080 localhost:5900 >/tmp/websockify.log 2>&1 &
"$CHROME" --user-data-dir="$BASE/profile" --no-sandbox --disable-dev-shm-usage \
  --window-size=1280,900 --no-first-run https://flow.google.com \
  >/tmp/chrome-login.log 2>&1 &
"$BASE/cloudflared" tunnel --url http://localhost:6080 >"$BASE/cloudflared.log" 2>&1 &
sleep 10
grep -o 'https://[^ ]*trycloudflare.com' "$BASE/cloudflared.log" | head -1 | tee "$BASE/login-url.txt"
echo "login stack running; stop with: $BASE/stop-login.sh"
