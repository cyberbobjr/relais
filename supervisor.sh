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
  ./supervisor.sh start all
  ./supervisor.sh stop all
  ./supervisor.sh restart all
  ./supervisor.sh reload all
  ./supervisor.sh status

Notes:
  - start all démarre supervisord si nécessaire puis lance tous les programmes.
  - reload all correspond à supervisorctl reload.
  - stop all arrête les programmes supervisés et coupe le démon supervisord.
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

wait_for_supervisord() {
    local retries=20

    for ((attempt=1; attempt<=retries; attempt++)); do
        if is_supervisord_running; then
            return 0
        fi

        sleep 0.25
    done

    echo "supervisord n'a pas créé de socket utilisable: $SOCKET_PATH" >&2
    if [[ -f "$PID_PATH" ]]; then
        echo "PID file détecté: $PID_PATH" >&2
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

    if is_supervisord_running; then
        return 0
    fi

    load_dotenv
    echo "Démarrage de supervisord..."
    supervisord -c "$CONFIG_PATH"
    wait_for_supervisord
}

run_supervisorctl() {
    supervisorctl -c "$CONFIG_PATH" "$@"
}

ACTION="${1:-}"
TARGET="${2:-}"

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
        ;;
    stop)
        if [[ "$TARGET" != "all" ]]; then
            usage
            exit 1
        fi
        if ! is_supervisord_running; then
            echo "supervisord n'est pas lancé. Rien à arrêter."
            exit 0
        fi
        echo "Arrêt de supervisord..."
        run_supervisorctl shutdown
        # Attendre que supervisord libère le socket (max 10s)
        stop_retries=40
        for ((attempt=1; attempt<=stop_retries; attempt++)); do
            if [[ ! -S "$SOCKET_PATH" ]]; then
                break
            fi
            sleep 0.25
        done
        if [[ -S "$SOCKET_PATH" ]]; then
            echo "Timeout: supervisord n'a pas libéré le socket dans les temps." >&2
            exit 1
        fi
        echo "supervisord arrêté."
        ;;
    restart)
        if [[ "$TARGET" != "all" ]]; then
            usage
            exit 1
        fi
        if is_supervisord_running; then
            echo "Arrêt de supervisord..."
            run_supervisorctl shutdown
            # Attendre que supervisord libère le socket (max 10s)
            stop_retries=40
            for ((attempt=1; attempt<=stop_retries; attempt++)); do
                if [[ ! -S "$SOCKET_PATH" ]]; then
                    break
                fi
                sleep 0.25
            done
            if [[ -S "$SOCKET_PATH" ]]; then
                echo "Timeout: supervisord n'a pas libéré le socket dans les temps." >&2
                exit 1
            fi
        fi
        load_dotenv
        echo "Démarrage de supervisord..."
        supervisord -c "$CONFIG_PATH"
        wait_for_supervisord
        run_supervisorctl start all
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
            echo "supervisord n'est pas lancé. Lancez: ./supervisor.sh start all"
            exit 1
        fi
        run_supervisorctl status
        ;;
    -h|--help|help|"")
        usage
        ;;
    *)
        usage
        exit 1
        ;;
esac