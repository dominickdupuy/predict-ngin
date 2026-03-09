#!/usr/bin/env bash
# Launch/manage the live whale strategy daemon in a screen session.
# Designed for HiPerGator (no systemd). Uses GNU screen.
#
# Usage:
#   bash scripts/live/launch.sh start [--capital 10000 --categories Geopolitics]
#   bash scripts/live/launch.sh stop
#   bash scripts/live/launch.sh status
#   bash scripts/live/launch.sh attach
#   bash scripts/live/launch.sh replay
#
# Note: run `chmod +x scripts/live/launch.sh` to make this script directly executable.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SESSION="whale-strategy"
PYTHON="${ROOT}/venv/bin/python3"
DAEMON="${ROOT}/scripts/live/run_live_strategy.py"
LOG_DIR="${ROOT}/logs/live"
LOG_FILE="${LOG_DIR}/daemon.log"

mkdir -p "${LOG_DIR}"

cmd="${1:-status}"
shift || true
extra_args="$*"

case "${cmd}" in
  start)
    if screen -ls "${SESSION}" 2>/dev/null | grep -q "${SESSION}"; then
      echo "Already running (screen session '${SESSION}' exists)."
      echo "Use: bash scripts/live/launch.sh attach"
      exit 0
    fi
    echo "Starting whale strategy daemon in screen session '${SESSION}'..."
    echo "Log: ${LOG_FILE}"
    screen -dmS "${SESSION}" bash -c "
      cd '${ROOT}'
      export PYTHONPATH=.:src
      exec '${PYTHON}' '${DAEMON}' \
        --research-dir data/research \
        --resolutions-dir data/poly_cat \
        ${extra_args} \
        2>&1 | tee -a '${LOG_FILE}'
    "
    sleep 1
    if screen -ls "${SESSION}" 2>/dev/null | grep -q "${SESSION}"; then
      echo "Started. PID: $(screen -ls ${SESSION} | grep -oP '\d+(?=\.${SESSION})')"
      echo ""
      echo "Commands:"
      echo "  Attach:  bash scripts/live/launch.sh attach"
      echo "  Status:  bash scripts/live/launch.sh status"
      echo "  Stop:    bash scripts/live/launch.sh stop"
      echo "  Logs:    tail -f ${LOG_FILE}"
    else
      echo "ERROR: Failed to start session."
      exit 1
    fi
    ;;

  stop)
    if screen -ls "${SESSION}" 2>/dev/null | grep -q "${SESSION}"; then
      screen -S "${SESSION}" -X quit
      echo "Stopped session '${SESSION}'."
    else
      echo "No running session '${SESSION}'."
    fi
    ;;

  status)
    if screen -ls "${SESSION}" 2>/dev/null | grep -q "${SESSION}"; then
      echo "RUNNING — screen session '${SESSION}' is active."
    else
      echo "STOPPED — no screen session '${SESSION}'."
    fi
    echo ""
    PYTHONPATH=.:src "${PYTHON}" scripts/live/health_check.py || true
    ;;

  attach)
    if screen -ls "${SESSION}" 2>/dev/null | grep -q "${SESSION}"; then
      screen -r "${SESSION}"
    else
      echo "No running session '${SESSION}'. Use: bash scripts/live/launch.sh start"
      exit 1
    fi
    ;;

  replay)
    echo "Starting replay (no orders placed)..."
    PYTHONPATH=.:src "${PYTHON}" "${DAEMON}" \
      --research-dir data/research \
      --resolutions-dir data/poly_cat \
      --replay \
      ${extra_args}
    ;;

  logs)
    tail -f "${LOG_FILE}"
    ;;

  *)
    echo "Usage: $0 {start|stop|status|attach|replay|logs} [extra args]"
    exit 1
    ;;
esac
