#!/bin/bash
# Stop the one-time login stack. Keeps Xvfb (the bridge service needs it) and
# the now-logged-in Chromium profile.
set -uo pipefail
BASE="$(cd "$(dirname "$0")" && pwd)"

pkill -f "cloudflared tunnel" 2>/dev/null
pkill -f "websockify.*6080" 2>/dev/null
pkill -f "x11vnc -display :99" 2>/dev/null
pkill -f -- "--user-data-dir=$BASE/profile" 2>/dev/null
rm -f "$BASE/vncpasswd" "$BASE/login-url.txt"
sleep 1

# Make sure the bridge (not the login browser) owns the profile again before
# anything else opens it.
sudo systemctl restart flowbridge 2>/dev/null || true
echo "login stack stopped; Xvfb :99 and the profile are kept"
