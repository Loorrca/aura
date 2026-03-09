#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$HOME/aura/aura_infer"
VENV="$APP_DIR/.venv"
PIDFILE="$APP_DIR/uvicorn.pid"
LOGFILE="$APP_DIR/uvicorn.log"
VERSION_FILE="$APP_DIR/model_version.txt"

get_version() {
  if [[ "${2:-}" != "" ]]; then
    echo "$2" > "$VERSION_FILE"
    echo "$2"
    return
  fi
  if [[ -f "$VERSION_FILE" ]]; then
    cat "$VERSION_FILE"
  else
    echo "1"
  fi
}


start() {
  if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "Already running (pid $(cat "$PIDFILE"))"
    exit 0
  fi

  ver="$(get_version start "${1:-}")"

  echo "Starting uvicorn..."
  nohup bash -lc "
    cd '$APP_DIR'
    source '$VENV/bin/activate'
    export AURA_MODEL_VERSION='$ver'
    exec python -m uvicorn app:app --host 0.0.0.0 --port 8000
  " >>"$LOGFILE" 2>&1 &

  echo $! >"$PIDFILE"
  echo "Started pid $(cat "$PIDFILE")"
}

stop() {
  if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "Stopping pid $(cat "$PIDFILE")"
    kill "$(cat "$PIDFILE")" || true
    sleep 1
  fi

  # Ensure nothing is left behind
  pkill -f "uvicorn app:app --host 0.0.0.0 --port 8000" 2>/dev/null || true
  rm -f "$PIDFILE"
  echo "Stopped"
}

restart() {
  stop
  start "${1:-}"
}

status() {
  if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "RUNNING pid $(cat "$PIDFILE")"
    exit 0
  fi
  echo "NOT RUNNING"
  exit 1
}

logs() {
  tail -n 50 "$LOGFILE" || true
}

cmd="${1:-}"
shift || true

case "$cmd" in
  start|stop|restart|status|logs) "$cmd" "$@" ;;
  *) echo "Usage: $0 {start|stop|restart|status|logs} [model_version]"; exit 2 ;;
esac
