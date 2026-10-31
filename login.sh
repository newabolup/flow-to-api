#!/bin/bash
# One-time Google login for the flow bridge, through a temporary noVNC page.
#
# The bridge reuses the Chromium profile in $BASE/profile, so this has to run
# a Chromium against that same profile once. You open the printed link on your
# own device, sign in to Google, and then run stop-login.sh.
#
# Security: the tunnel is public, so this script generates a random VNC
# password and prints it with the link. Do not share the link; stop the stack
# as soon as you are done.
set -uo pipefail
BASE="$(cd "$(dirname "$0")" && pwd)"

CHROME="$(ls -d "$HOME"/.cache/ms-playwright/chromium-*/chrome-linux*/chrome 2>/dev/null | head -1)"
[ -n "$CHROME" ] || { echo "chromium binary not found; run install.sh first"; exit 1; }
export DISPLAY=:99

if ! pgrep -f "Xvfb :99" >/dev/null; then
  Xvfb :99 -screen 0 1280x900x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
  sleep 1
fi

VNC_PASS="$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 12)"
x11vnc -storepasswd "$VNC_PASS" "$BASE/vncpasswd" >/dev/null 2>&1
x11vnc -display :99 -forever -shared -rfbport 5900 -localhost \
  -rfbauth "$BASE/vncpasswd" >/tmp/x11vnc.log 2>&1 &

if [ -d "$BASE/novnc" ]; then
  # Bind to localhost: only cloudflared (below) may reach it.
  websockify --web "$BASE/novnc" localhost:6080 localhost:5900 >/tmp/websockify.log 2>&1 &
fi

# Kill a leftover login browser first: two Chromiums cannot share a profile.
pkill -f -- "--user-data-dir=$BASE/profile" 2>/dev/null
sleep 1
"$CHROME" --user-data-dir="$BASE/profile" --no-sandbox --disable-dev-shm-usage \
  --password-store=basic --no-first-run --no-default-browser-check \
  --window-size=1280,900 https://flow.google.com >/tmp/chrome-login.log 2>&1 &

if [ -x "$BASE/cloudflared" ] && [ -d "$BASE/novnc" ]; then
  "$BASE/cloudflared" tunnel --url http://localhost:6080 >"$BASE/cloudflared.log" 2>&1 &
  sleep 10
  URL="$(grep -o 'https://[^ ]*trycloudflare.com' "$BASE/cloudflared.log" | head -1)"
  echo "$URL" > "$BASE/login-url.txt"
  echo
  echo "  Open this link on your phone or laptop: $URL"
  echo "  VNC password:                          $VNC_PASS"
  echo
  echo "  Sign in to Google, open Flow, then run: $BASE/stop-login.sh"
else
  echo "noVNC/cloudflared not installed: connect a display to :99 yourself and log in there."
fi
