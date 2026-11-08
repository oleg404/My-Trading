#!/bin/bash
# TradingAgents desk at http://127.0.0.1:8787
#   scripts/dashboard.sh            start (if not running) and open in browser
#   scripts/dashboard.sh stop       stop it
#   scripts/dashboard.sh status     is it up?
#   scripts/dashboard.sh install    auto-start at login and restart on crash (launchd)
#   scripts/dashboard.sh uninstall  remove auto-start
#   scripts/dashboard.sh fg         run in the foreground (Ctrl+C to stop)
set -euo pipefail

SCRIPT="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
APP="$(cd "$(dirname "$0")/../tradingagents" && pwd)"
PORT="${TRADINGAGENTS_DASHBOARD_PORT:-8787}"
URL="http://127.0.0.1:$PORT/"
LOG="$HOME/.tradingagents/dashboard/server.log"
PY="$APP/.venv/bin/python"
LABEL="com.tradingagents.dashboard"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

up() { /usr/bin/curl -s -m 1 -o /dev/null "$URL"; }
pid() { /usr/sbin/lsof -t -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true; }
loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }
wait_up() { for _ in $(seq 1 120); do up && return 0; sleep 0.25; done; return 1; }
wait_down() { for _ in $(seq 1 40); do up || return 0; sleep 0.25; done; return 1; }
started() {
  if wait_up; then echo "Started: $URL (log: $LOG)"; else echo "Failed to start, see $LOG"; tail -20 "$LOG"; exit 1; fi
}

if [ ! -x "$PY" ]; then
  echo "No venv yet. Run: cd \"$APP\" && uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python -e ."
  exit 1
fi
mkdir -p "$(dirname "$LOG")"

case "${1:-start}" in
  start)
    if up; then
      echo "Already running: $URL"
    elif [ -f "$PLIST" ]; then
      loaded || launchctl bootstrap "$DOMAIN" "$PLIST"
      launchctl kickstart "$DOMAIN/$LABEL"
      started
    else
      cd "$APP"
      PYTHONWARNINGS=ignore nohup "$PY" dashboard/server.py >>"$LOG" 2>&1 &
      started
    fi
    open "$URL"
    ;;
  stop)
    # Unload first, or launchd would restart it right away.
    if loaded; then launchctl bootout "$DOMAIN/$LABEL" && echo "Stopped (auto-start resumes at next login)"; fi
    p="$(pid)"
    if [ -n "$p" ]; then kill $p && echo "Stopped (pid $p)"; elif ! [ -f "$PLIST" ]; then echo "Not running"; fi
    wait_down || true
    ;;
  status)
    if up; then echo "Running: $URL (pid $(pid))"; else echo "Not running"; fi
    if [ -f "$PLIST" ]; then echo "Auto-start: on"; else echo "Auto-start: off"; fi
    ;;
  install)
    "$SCRIPT" stop >/dev/null
    mkdir -p "$(dirname "$PLIST")"
    cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$SCRIPT</string><string>fg</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
</dict>
</plist>
EOF
    launchctl bootstrap "$DOMAIN" "$PLIST"
    started
    echo "Auto-start installed: $PLIST"
    ;;
  uninstall)
    loaded && launchctl bootout "$DOMAIN/$LABEL" || true
    rm -f "$PLIST"
    echo "Auto-start removed (dashboard stopped; run '$0' to start it manually)"
    ;;
  fg)
    cd "$APP" && PYTHONWARNINGS=ignore exec "$PY" dashboard/server.py
    ;;
  *)
    echo "Usage: $0 [start|stop|status|install|uninstall|fg]"; exit 1
    ;;
esac
