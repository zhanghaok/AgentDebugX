#!/usr/bin/env bash
# Local DataAgent diagnosis service lifecycle: start | stop | restart | status | logs.
#
# Replica of the intranet AgentDebugX diagnosis service (FastAPI on :7777).
# Serves a native session/diagnosis console at http://127.0.0.1:7777/.
# Reads the OpenCode SQLite store of the local dataagent runtime and reuses
# the DeepSeek credentials from dataagent/.local-dev/config/local.env
# (GTS_LLM_* -> AGENTDEBUG_LLM_*). Never commit that file.
#
# Usage:
#   scripts/local-diagnosis-service.sh start|stop|restart|status|logs [N]
# Env overrides:
#   DATAAGENT_HOME   dataagent checkout (default ~/develop/dataagent)
#   DATAAGENT_ENV_FILE  env file   (default $DATAAGENT_HOME/.local-dev/config/local.env)
#   OPENCODE_DB      SQLite store  (default $DATAAGENT_HOME/.local-dev/runtime/opencode.db)
#   DIAGNOSIS_PORT   listen port   (default 7777)
#   AGENTDEBUG_MIN_CONFIDENCE  root-cause threshold (default 0.5)
#   AGENTDEBUG_LANGUAGE        zh|en prompts     (default zh)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
RUN_DIR="$ROOT/.run"
LOG_FILE="$RUN_DIR/diagnosis.log"
PID_FILE="$RUN_DIR/diagnosis.pid"
PORT="${DIAGNOSIS_PORT:-7777}"
BASE_URL="http://127.0.0.1:$PORT"

DATAAGENT_HOME="${DATAAGENT_HOME:-$HOME/develop/dataagent}"
ENV_FILE="${DATAAGENT_ENV_FILE:-$DATAAGENT_HOME/.local-dev/config/local.env}"
export OPENCODE_DB="${OPENCODE_DB:-$DATAAGENT_HOME/.local-dev/runtime/opencode.db}"
export DIAGNOSIS_OUT_DIR="${DIAGNOSIS_OUT_DIR:-$ROOT/diagnosis}"
export DIAGNOSIS_PORT="$PORT"

load_env() {
  if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
  else
    echo "warn: $ENV_FILE not found; LLM stays unconfigured" >&2
  fi
  export AGENTDEBUG_LLM_BASE_URL="${AGENTDEBUG_LLM_BASE_URL:-${GTS_LLM_BASE_URL:-}}"
  export AGENTDEBUG_LLM_API_KEY="${AGENTDEBUG_LLM_API_KEY:-${GTS_LLM_API_KEY:-}}"
  export AGENTDEBUG_LLM_MODEL="${AGENTDEBUG_LLM_MODEL:-deepseek-flash}"
  export NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,$NO_PROXY}"
  export no_proxy="127.0.0.1,localhost${no_proxy:+,$no_proxy}"
}

pid_of() {
  [ -f "$PID_FILE" ] && cat "$PID_FILE"
}

is_running() {
  local pid
  pid="$(pid_of)"
  [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null
}

start() {
  load_env
  if is_running; then
    echo "already running (pid $(pid_of)) on $BASE_URL"
    return 0
  fi
  if [ ! -x "$PY" ]; then
    echo "venv missing. Run:" >&2
    echo "  uv venv --python 3.12 .venv && uv pip install -e '.[ui]'" >&2
    exit 1
  fi
  mkdir -p "$RUN_DIR"
  nohup "$PY" -m agentdebug.api >> "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"
  local i
  for i in $(seq 1 40); do
    if curl -fsS "$BASE_URL/healthz" >/dev/null 2>&1; then
      echo "started (pid $(pid_of)) on $BASE_URL"
      echo "console: $BASE_URL/"
      curl -fsS "$BASE_URL/healthz"; echo
      return 0
    fi
    sleep 0.25
  done
  echo "failed to start; last log lines:" >&2
  tail -n 20 "$LOG_FILE" >&2 || true
  exit 1
}

stop() {
  if ! is_running; then
    echo "not running"
    rm -f "$PID_FILE"
    return 0
  fi
  local pid i
  pid="$(pid_of)"
  kill "$pid" 2>/dev/null || true
  for i in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.25
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  echo "stopped"
}

status() {
  if is_running; then
    echo "running (pid $(pid_of)) on $BASE_URL"
    echo "console: $BASE_URL/"
    curl -fsS "$BASE_URL/healthz" 2>/dev/null || echo "(healthz unreachable)"
    echo
  else
    echo "not running"
  fi
}

logs() {
  tail -n "${1:-80}" "$LOG_FILE"
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  logs) logs "${2:-80}" ;;
  *)
    echo "usage: $0 start|stop|restart|status|logs [N]" >&2
    exit 2
    ;;
esac
