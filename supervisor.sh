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
  ./supervisor.sh [--verbose] restart <service>
  ./supervisor.sh stop all
  ./supervisor.sh stop <service>
  ./supervisor.sh reload all
  ./supervisor.sh status
  ./supervisor.sh clear
  ./supervisor.sh force-kill

Options:
  --verbose   After startup, follows logs of all bricks in real time.
              Ctrl+C detaches logs without stopping supervisord.

Notes:
  - start all starts supervisord if needed then launches all programs.
  - restart <service> restarts a single service (e.g. aiguilleur, atelier).
  - stop <service> stops a single service without shutting down supervisord.
  - reload all corresponds to supervisorctl reload.
  - stop all stops supervised programs and shuts down the supervisord daemon.
  - clear removes all files in .relais/logs.
  - force-kill kills every RELAIS process (supervisord, courier/redis,
    launchers) including zombies and orphans reparented to init, then
    cleans up stale sockets and pidfiles. Use when normal stop is stuck.
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
    # Use `pid` (not `status`): supervisorctl status returns exit code 3 as
    # soon as ANY program is not RUNNING (e.g. autostart=false programs like
    # baileys-api), which would make us think the daemon is unreachable even
    # though it is perfectly responsive. `pid` only cares about the daemon.
    [[ -S "$SOCKET_PATH" ]] && supervisorctl -c "$CONFIG_PATH" pid >/dev/null 2>&1
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

    # Rule 1: pidfile points to a live process → not stale
    if pid="$(get_pidfile_pid 2>/dev/null)" && is_pid_running "$pid"; then
        return 0
    fi

    # Rule 2: socket is responsive via supervisorctl → not stale
    # (use `pid` not `status`: see is_supervisord_running for rationale)
    if [[ -S "$SOCKET_PATH" ]] && run_supervisorctl pid >/dev/null 2>&1; then
        return 0
    fi

    # Rule 3: any supervisord process for this config is alive → not stale.
    # This catches the narrow window between `supervisord -c` returning (parent
    # exited via os._exit after fork) and the child having written its pidfile.
    # Without this check we would race and delete the socket the freshly-forked
    # child just created — leaving it running headless with no reachable socket.
    if [[ -n "$(list_supervisord_pids)" ]]; then
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

    # NOTE: do NOT call cleanup_stale_artifacts here. At this point supervisord
    # has just forked and the parent returned — the child may not have written
    # its pidfile yet, and its freshly-created socket is not yet accepting on
    # runforever(). Cleaning up here would rm the socket the child just made.
    # Cleanup is the responsibility of ensure_supervisord_running, BEFORE start.

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

validate_service_name() {
    local name="$1"
    if [[ ! "$name" =~ ^[a-zA-Z0-9_:.-]+$ ]]; then
        echo "Invalid service name: '$name'. Only alphanumeric characters, '_', ':', '.', and '-' are allowed." >&2
        exit 1
    fi
}

clear_logs() {
    local logs_dir="$REPO_ROOT/.relais/logs"

    mkdir -p "$logs_dir"
    find "$logs_dir" -mindepth 1 \( -type f -o -type l \) -delete
    echo "Logs deleted in $logs_dir."
}

list_relais_orphans_by_cwd() {
    # Lists PIDs whose cwd is inside $REPO_ROOT/.relais/. Catches orphan
    # redis-server, supervisord, launchers, and any process that outlived
    # its RELAIS parent (reparented to PID 1). macOS `ps` does not expose
    # cwd, so we go through lsof.
    local marker="$REPO_ROOT/.relais"
    lsof -d cwd 2>/dev/null | awk -v m="$marker" '
        NR > 1 && index($NF, m) == 1 { print $2 }
    ' | sort -u || true
}

force_kill_launchers() {
    local killed=0
    local pid
    local cmd

    # 1. Try graceful supervisord stop first to prevent autorestart during kill
    if is_supervisord_running; then
        echo "supervisord is active — attempting graceful stop first..." >&2
        if stop_supervisord; then
            echo "supervisord stopped."
        else
            echo "Graceful stop failed — will SIGKILL below." >&2
        fi
    fi

    # 2. SIGKILL any supervisord still alive for this config (zombie shutdowns,
    #    orphans, unresponsive instances)
    while IFS= read -r pid; do
        [[ -n "$pid" ]] || continue
        if is_pid_running "$pid"; then
            echo "Force-killing supervisord (PID $pid)..."
            kill -9 "$pid" >/dev/null 2>&1 || true
            killed=$((killed + 1))
        fi
    done < <(list_supervisord_pids)

    # 3. Kill any RELAIS orphan by cwd (redis-server reparented to init,
    #    zombie launchers, anything spawned from .relais/ that outlived
    #    its parent). Safer than blind pkill — only touches processes
    #    running out of this repo's .relais/ directory.
    while IFS= read -r pid; do
        [[ -n "$pid" ]] || continue
        if is_pid_running "$pid"; then
            cmd="$(ps -o command= -p "$pid" 2>/dev/null || echo '?')"
            echo "Force-killing RELAIS orphan (PID $pid): $cmd"
            kill -9 "$pid" >/dev/null 2>&1 || true
            killed=$((killed + 1))
        fi
    done < <(list_relais_orphans_by_cwd)

    # 4. Belt-and-braces: python launcher processes matched by command name
    while IFS= read -r pid; do
        [[ -n "$pid" ]] || continue
        if is_pid_running "$pid"; then
            echo "Force-killing launcher process (PID $pid)..."
            kill -9 "$pid" >/dev/null 2>&1 || true
            killed=$((killed + 1))
        fi
    done < <(pgrep -if "python.*launcher" 2>/dev/null || true)

    # 5. Free debugpy ports (5670-5689 only, LISTEN state)
    while IFS= read -r pid; do
        [[ -n "$pid" ]] || continue
        if is_pid_running "$pid"; then
            echo "Force-killing process on debugpy port (PID $pid)..."
            kill -9 "$pid" >/dev/null 2>&1 || true
            killed=$((killed + 1))
        fi
    done < <(lsof -i TCP:5670-5689 -sTCP:LISTEN -P -n 2>/dev/null | awk 'NR>1 {print $2}' | sort -u)

    # 6. Cleanup stale artifacts — socket file (incl. .PID leftovers from a
    #    crashed supervisord prebind), pidfile, and orphaned redis socket.
    rm -f "$SOCKET_PATH" "$SOCKET_PATH".* "$PID_PATH" "$REPO_ROOT/.relais/redis.sock"
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
        run_supervisorctl start infra:* core:* relays:*
        if [[ "$VERBOSE" == 1 ]]; then tail_logs; fi
        ;;
    stop)
        if [[ "$TARGET" == "all" ]]; then
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
        elif [[ -n "$TARGET" ]]; then
            validate_service_name "$TARGET"
            ensure_supervisord_running
            run_supervisorctl stop "$TARGET"
        else
            usage
            exit 1
        fi
        ;;
    restart)
        if [[ "$TARGET" == "all" ]]; then
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
            run_supervisorctl start infra:* core:* relays:*
            if [[ "$VERBOSE" == 1 ]]; then tail_logs; fi
        elif [[ -n "$TARGET" ]]; then
            validate_service_name "$TARGET"
            ensure_supervisord_running
            run_supervisorctl restart "$TARGET"
            if [[ "$VERBOSE" == 1 ]]; then tail_logs; fi
        else
            usage
            exit 1
        fi
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
