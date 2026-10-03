#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 || ! $2 =~ ^[0-9]+$ || ! $3 =~ ^\$[0-9]+$ ]]; then
    echo 'Usage: attach-environment.sh SOCKET CLIENT_PID SESSION_ID' >&2
    exit 2
fi
socket=$1
client_pid=$2
session_id=$3
[[ -O /proc/$client_pid && -r /proc/$client_pid/environ ]] || exit 0
root=$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")/.." && pwd)
binary="$root/build/runtime/tmux"

# The hook may outlive its client. Check the PID/session pair in the server queue.
guard="#{L:#{?#{&&:#{==:#{client_pid},${client_pid}},#{==:#{session_id},${session_id}}},1,}}"
while IFS= read -r -d '' entry; do
    name=${entry%%=*}
    case "$name" in
    DISPLAY | WAYLAND_DISPLAY | XAUTHORITY)
        value=${entry#*=}
        [[ -n $value ]] || continue
        # Quote tmux's command language, not a shell or an eval expression.
        quoted=${value//\'/\'\\\'\'}
        command="set-environment -t '${session_id}' '${name}' '${quoted}'"
        "$binary" -N -S "$socket" if-shell -F "$guard" "$command"
        ;;
    esac
done <"/proc/$client_pid/environ"
