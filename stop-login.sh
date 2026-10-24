#!/bin/bash
# Stop the one-time login stack, keeping Xvfb (for the bridge) and the
# logged-in Chromium profile. Run after the owner finishes Google login.
pkill -f "cloudflared tunnel" 2>/dev/null
pkill -f "websockify.*6080" 2>/dev/null
pkill -f "x11vnc -display :99" 2>/dev/null
pkill -f "flowbridge/profile" 2>/dev/null
sleep 1
echo "login stack stopped; Xvfb :99 left running for the bridge service"
