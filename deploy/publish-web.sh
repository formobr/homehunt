#!/usr/bin/env bash
# Render the journal page and copy it to the web host. Never fails the poll.
# HOMEHUNT_WEB_SSH / HOMEHUNT_WEB_DIR come from the environment or the repo's .env.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(dirname "$here")"
out="$root/data/web/index.html"

# Read KEY from .env (plain KEY=VALUE lines; never sourced).
env_file_value() {
    local line key value
    [ -f "$root/.env" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line#"${line%%[![:space:]]*}"}"
        case "$line" in "$1="*) ;; *) continue ;; esac
        value="${line#*=}"
        value="${value%"${value##*[![:space:]]}"}"
        case "$value" in
            \"*\") value="${value:1:${#value}-2}" ;;
            \'*\') value="${value:1:${#value}-2}" ;;
        esac
        printf '%s' "$value"
    done < "$root/.env"
}

host="${HOMEHUNT_WEB_SSH:-}"
[ -n "$host" ] || host="$(env_file_value HOMEHUNT_WEB_SSH)"
dir="${HOMEHUNT_WEB_DIR:-}"
[ -n "$dir" ] || dir="$(env_file_value HOMEHUNT_WEB_DIR)"

if ! (cd "$root" && "$root/.venv/bin/homehunt" web --out "$out") >/dev/null; then
    echo "publish-web.sh: render failed; skipping publish" >&2
    exit 0
fi

if [ -z "$host" ]; then
    echo "publish skipped: HOMEHUNT_WEB_SSH not set" >&2
    exit 0
fi
if [ -z "$dir" ]; then
    echo "publish skipped: HOMEHUNT_WEB_DIR not set" >&2
    exit 0
fi

if ! ssh -o BatchMode=yes -o ConnectTimeout=15 "$host" \
    "mkdir -p '$dir' && cat > '$dir/index.html.tmp' && mv '$dir/index.html.tmp' '$dir/index.html'" \
    < "$out"; then
    echo "publish-web.sh: upload to $host:$dir failed" >&2
fi
exit 0
