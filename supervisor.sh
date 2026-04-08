#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"
CONFIG_PATH="$REPO_ROOT/supervisord.conf"
SOCKET_PATH="$REPO_ROOT/.relais/supervisor.sock"
PID_PATH="$REPO_ROOT/.relais/supervisord.pid"

usage() {
    cat <<'EOF'
Usage:
  ./supervisor.sh [--verbose] start all
  ./supervisor.sh [--verbose] restart all
  ./supervisor.sh stop all
  ./supervisor.sh reload all
  ./supervisor.sh status
  ./supervisor.sh clear
  ./supervisor.sh force-kill

Options:
  --verbose   After startup, follows logs of all bricks in real time.
              Ctrl+C detaches logs without stopping supervisord.

Notes:
  - start all starts supervisord if needed then launches all programs.
  - reload all corresponds to supervisorctl reload.
  - stop all stops supervised programs and shuts down the supervisord daemon.
  - clear removes all files in .relais/logs.
  - force-kill kills all orphan launcher processes blocking debugpy ports.
EOF
}

require_command() {
    local command_name="$1"

    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "Commande introuvable: $command_name" >&2
        exit 1
    fi
}

is_supervisord_running() {
    [[ -S "$SOCKET_PATH" ]] && supervisorctl -c "$CONFIG_PATH" status >/dev/null 2>&1
}

get_pidfile_pid() {
    [[ -f "$PID_PATH" ]] || return 1

    local pid
    pid="$(tr -d '[:space:]' < "$PID_PATH")"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    printf '%s\n' "$pid"
}

get_active_supervisord_pid() {
    [[ -S "$SOCKET_PATH" ]] || return 1

    local pid
    pid="$(run_supervisorctl pid 2>/dev/null | tr -d '[:space:]')" || return 1
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    printf '%s\n' "$pid"
}

is_pid_running() {
    local pid="${1:-}"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" >/dev/null 2>&1
}

wait_for_pid_exit() {
    local pid="$1"
    local retries="${2:-40}"
    local delay="${3:-0.25}"

    for ((attempt=1; attempt<=retries; attempt++)); do
        if ! is_pid_running "$pid"; then
            return 0
        fi

        sleep "$delay"
    done

    return 1
}

list_supervisord_pids() {
    ps -ax -o pid= -o command= | awk -v config="$CONFIG_PATH" '
        index($0, "supervisord") && index($0, " -c " config) { print $1 }
    '
}

cleanup_stale_artifacts() {
    local pid

    if pid="$(get_pidfile_pid 2>/dev/null)" && is_pid_running "$pid"; then
        return 0
    fi

    if [[ -S "$SOCKET_PATH" ]] && run_supervisorctl status >/dev/null 2>&1; then
        return 0
    fi

    rm -f "$SOCKET_PATH" "$PID_PATH"
}

force_stop_pid() {
    local pid="$1"

    if ! is_pid_running "$pid"; then
        return 0
    fi

    echo "supervisord did not exit after shutdown; sending SIGTERM..." >&2
    kill "$pid" >/dev/null 2>&1 || true
    if wait_for_pid_exit "$pid" 20 0.25; then
        return 0
    fi

    echo "supervisord resists SIGTERM; sending SIGKILL..." >&2
    kill -9 "$pid" >/dev/null 2>&1 || true
    wait_for_pid_exit "$pid" 20 0.25
}

stop_orphaned_supervisords() {
    local pid
    local failed=0

    while IFS= read -r pid; do
        [[ -n "$pid" ]] || continue

        if ! is_pid_running "$pid"; then
            continue
        fi

        echo "Stopping orphan supervisord (PID $pid)..." >&2
        kill "$pid" >/dev/null 2>&1 || true
        if wait_for_pid_exit "$pid" 20 0.25; then
            continue
        fi

        echo "Orphan supervisord (PID $pid) resists SIGTERM; sending SIGKILL..." >&2
        kill -9 "$pid" >/dev/null 2>&1 || true
        if ! wait_for_pid_exit "$pid" 20 0.25; then
            echo "Timeout: orphan supervisord (PID $pid) could not be stopped." >&2
            failed=1
        fi
    done < <(list_supervisord_pids)

    return "$failed"
}

stop_supervisord() {
    local max_rounds=5
    local active_pid

    cleanup_stale_artifacts

    for ((round=1; round<=max_rounds; round++)); do
        if ! is_supervisord_running; then
            if ! stop_orphaned_supervisords; then
                return 1
            fi
            cleanup_stale_artifacts
            return 0
        fi

        active_pid="$(get_active_supervisord_pid 2>/dev/null || true)"

        echo "Stopping supervisord..."
        run_supervisorctl shutdown || true

        if [[ -n "$active_pid" ]]; then
            if ! wait_for_pid_exit "$active_pid" 40 0.25; then
                if ! force_stop_pid "$active_pid"; then
                    echo "Timeout: supervisord (PID $active_pid) did not exit in time." >&2
                    return 1
                fi
            fi
        fi

        cleanup_stale_artifacts

        if ! is_supervisord_running; then
            if ! stop_orphaned_supervisords; then
                return 1
            fi
            cleanup_stale_artifacts
            return 0
        fi
    done

    echo "Timeout: supervisord still reachable after multiple stop attempts." >&2
    return 1
}

wait_for_supervisord() {
    local retries=20

    cleanup_stale_artifacts

    for ((attempt=1; attempt<=retries; attempt++)); do
        if is_supervisord_running; then
            return 0
        fi

        sleep 0.25
    done

    echo "supervisord did not create a usable socket: $SOCKET_PATH" >&2
    if [[ -f "$PID_PATH" ]]; then
        echo "PID file detected: $PID_PATH" >&2
    fi
    exit 1
}

load_dotenv() {
    local env_file="$REPO_ROOT/.env"
    if [[ -f "$env_file" ]]; then
        set -o allexport
        # shellcheck source=/dev/null
        source "$env_file"
        set +o allexport
    fi
}

ensure_supervisord_running() {
    mkdir -p "$REPO_ROOT/.relais/logs"
    cleanup_stale_artifacts

    if is_supervisord_running; then
        return 0
    fi

    if ! stop_orphaned_supervisords; then
        return 1
    fi
    cleanup_stale_artifacts

    load_dotenv
    echo "Starting supervisord..."
    supervisord -c "$CONFIG_PATH"
    wait_for_supervisord
}

run_supervisorctl() {
    supervisorctl -c "$CONFIG_PATH" "$@"
}

clear_logs() {
    local logs_dir="$REPO_ROOT/.relais/logs"

    mkdir -p "$logs_dir"
    find "$logs_dir" -mindepth 1 \( -type f -o -type l \) -delete
    echo "Logs deleted in $logs_dir."
}

force_kill_launchers() {
    local killed=0
    local pid

    # Stop supervisord first — otherwise autorestart will respawn killed processes immediately
    if is_supervisord_running; then
        echo "supervisord is active — stopping first to prevent respawn..." >&2
        if ! stop_supervisord; then
            echo "Unable to stop supervisord. Aborting." >&2
            return 1
        fi
        echo "supervisord stopped."
    fi

    # Kill all python launcher processes
    while IFS= read -r pid; do
        [[ -n "$pid" ]] || continue
        if is_pid_running "$pid"; then
            echo "Force-killing launcher process (PID $pid)..."
            kill -9 "$pid" >/dev/null 2>&1 || true
            killed=$((killed + 1))
        fi
    done < <(pgrep -if "python.*launcher" 2>/dev/null || true)

    # Kill all processes listening on debugpy ports (5670-5689 only, LISTEN state)
    while IFS= read -r pid; do
        [[ -n "$pid" ]] || continue
        if is_pid_running "$pid"; then
            echo "Force-killing process on debugpy port (PID $pid)..."
            kill -9 "$pid" >/dev/null 2>&1 || true
            killed=$((killed + 1))
        fi
    done < <(lsof -i TCP:5670-5689 -sTCP:LISTEN -P -n 2>/dev/null | awk 'NR>1 {print $2}' | sort -u)

    # Cleanup stale artifacts
    cleanup_stale_artifacts

    if [[ $killed -gt 0 ]]; then
        echo "$killed orphan processes terminated. Debugpy ports freed."
        sleep 1
    else
        echo "No orphan processes detected."
    fi
}

VERBOSE=0
POSITIONAL=()
for arg in "$@"; do
    if [[ "$arg" == "--verbose" ]]; then
        VERBOSE=1
    else
        POSITIONAL+=("$arg")
    fi
done

ACTION="${POSITIONAL[0]:-}"
TARGET="${POSITIONAL[1]:-}"

tail_logs() {
    local logs_dir="$REPO_ROOT/.relais/logs"
    echo "Verbose mode — following logs (Ctrl+C to detach)..."
    sleep 0.5
    # shellcheck disable=SC2012
    if ls "$logs_dir"/*.log >/dev/null 2>&1; then
        tail -f "$logs_dir"/*.log
    else
        echo "No .log files found in $logs_dir." >&2
    fi
}

require_command supervisord
require_command supervisorctl

case "$ACTION" in
    start)
        if [[ "$TARGET" != "all" ]]; then
            usage
            exit 1
        fi
        ensure_supervisord_running
        run_supervisorctl start all
        if [[ "$VERBOSE" == 1 ]]; then tail_logs; fi
        ;;
    stop)
        if [[ "$TARGET" != "all" ]]; then
            usage
            exit 1
        fi
        cleanup_stale_artifacts
        if ! is_supervisord_running; then
            if ! stop_orphaned_supervisords; then
                exit 1
            fi
            cleanup_stale_artifacts
            echo "supervisord is not running. Nothing to stop."
            exit 0
        fi
        if ! stop_supervisord; then
            exit 1
        fi
        echo "supervisord stopped."
        ;;
    restart)
        if [[ "$TARGET" != "all" ]]; then
            usage
            exit 1
        fi
        cleanup_stale_artifacts
        if is_supervisord_running; then
            if ! stop_supervisord; then
                exit 1
            fi
        fi
        load_dotenv
        echo "Starting supervisord..."
        supervisord -c "$CONFIG_PATH"
        wait_for_supervisord
        run_supervisorctl start all
        if [[ "$VERBOSE" == 1 ]]; then tail_logs; fi
        ;;
    reload)
        if [[ "$TARGET" != "all" ]]; then
            usage
            exit 1
        fi
        ensure_supervisord_running
        run_supervisorctl reload
        ;;
    status)
        if [[ -n "$TARGET" ]]; then
            usage
            exit 1
        fi
        if ! is_supervisord_running; then
            echo "supervisord is not running. Run: ./supervisor.sh start all"
            exit 1
        fi
        run_supervisorctl status
        ;;
    clear)
        if [[ -n "$TARGET" ]]; then
            usage
            exit 1
        fi
        clear_logs
        ;;
    force-kill)
        if [[ -n "$TARGET" ]]; then
            usage
            exit 1
        fi
        force_kill_launchers
        ;;
    -h|--help|help|"")
        usage
        ;;
    *)
        usage
        exit 1
        ;;
esac
