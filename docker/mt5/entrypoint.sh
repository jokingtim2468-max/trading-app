#!/usr/bin/env bash
# Start a private virtual display, the MT5 terminal (portable mode, so EAs/indicators live
# under the install folder that the bridge mounts read-only), then the RPyC server.
set -euo pipefail
TERMINAL="C:\\Program Files\\MetaTrader 5\\terminal64.exe"

rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1280x800x16 -nolisten tcp -nolisten unix >/dev/null 2>&1 &
sleep 2

cleanup() { wineserver -k 2>/dev/null || true; }
trap cleanup EXIT TERM INT

echo "[mt5] starting terminal (headless)"
wine "$TERMINAL" /portable >/dev/null 2>&1 &

echo "[mt5] starting RPyC server on :18812"
exec wine 'C:\Python\python.exe' /home/trader/rpyc_server.py
