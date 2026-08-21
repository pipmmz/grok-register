#!/bin/sh
set -eu

cd /app

# Ensure runtime dirs exist (mounted volumes may start empty).
mkdir -p /data /data/cpa_auths /data/screenshots

# Prefer mounted config; bootstrap from example on first run.
if [ ! -f /app/config.json ]; then
  if [ -f /data/config.json ]; then
    ln -sfn /data/config.json /app/config.json
  elif [ -f /app/config.example.json ]; then
    cp /app/config.example.json /data/config.json
    ln -sfn /data/config.json /app/config.json
    echo "[*] bootstrapped /data/config.json from config.example.json"
  fi
fi

# Keep relative output paths on the data volume.
if [ ! -e /app/cpa_auths ]; then
  ln -sfn /data/cpa_auths /app/cpa_auths
fi
if [ ! -e /app/screenshots ]; then
  ln -sfn /data/screenshots /app/screenshots
fi

# Persist account files under /data when running in container.
export GROK_REGISTER_DATA_DIR="${GROK_REGISTER_DATA_DIR:-/data}"

# Virtual display for headed Chromium (required for registration).
export DISPLAY="${DISPLAY:-:99}"
XVFB_WHD="${XVFB_WHD:-1280x720x24}"

if ! pgrep -x Xvfb >/dev/null 2>&1; then
  Xvfb "$DISPLAY" -screen 0 "$XVFB_WHD" -ac +extension GLX +render -noreset \
    >/tmp/xvfb.log 2>&1 &
  # Wait briefly for the display socket.
  i=0
  while [ "$i" -lt 50 ]; do
    if [ -e "/tmp/.X11-unix/X${DISPLAY#:}" ] || [ -e "/tmp/.X${DISPLAY#:}-lock" ]; then
      break
    fi
    i=$((i + 1))
    sleep 0.1
  done
fi

MODE="${1:-web}"
shift || true

case "$MODE" in
  web|webui|server)
    export GROK_REGISTER_HOST="${GROK_REGISTER_HOST:-0.0.0.0}"
    export GROK_REGISTER_PORT="${GROK_REGISTER_PORT:-8092}"
    exec python -m web.server "$@"
    ;;
  cli)
    exec python grok_register_ttk.py cli "$@"
    ;;
  start)
    exec python grok_register_ttk.py start "$@"
    ;;
  retry-pending)
    exec python grok_register_ttk.py retry-pending "$@"
    ;;
  shell|bash|sh)
    exec /bin/sh "$@"
    ;;
  *)
    exec "$MODE" "$@"
    ;;
esac
