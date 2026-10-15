#!/bin/sh
# Start Kyora in iSH on iOS.
#
# Usage:  sh launchers/kyora-ish.sh [serve | selfcheck | bench | version] [args...]
#
#   (no command)  serve: start the local server; then open Safari at http://localhost:8765
#   other args    passed to Kyora unchanged, e.g.  sh launchers/kyora-ish.sh serve --port 9000
#
# iSH cannot be a Hub (no Claude Code on iOS); link this phone to a PC Hub instead.
# Release layout: <root>/kyora.pyz -> "python3 <root>/kyora.pyz serve"; a source checkout runs
# "python3 -m kyora". The script prints no keys or tokens and exits with Kyora's exit code.
set -eu

usage() {
    echo "Usage: sh launchers/kyora-ish.sh [serve | selfcheck | bench | version] [args...]"
}

KYORA_HOME="${KYORA_HOME:-$HOME/.kyora}"
export KYORA_HOME

script_dir=$(CDPATH='' cd "$(dirname "$0")" && pwd)
root=$(dirname "$script_dir")

cmd=serve
if [ $# -gt 0 ]; then
    case "$1" in
        serve|selfcheck|bench|version) cmd=$1; shift ;;
        hub|--hub)
            echo "iSH cannot run Claude Code, so it cannot be a Hub. Run the Hub on your PC and link this phone." >&2
            exit 2 ;;
        -h|--help|help) usage; exit 0 ;;
        -*) cmd=serve ;;
        *) echo "kyora-ish.sh: unknown command '$1'" >&2; usage >&2; exit 2 ;;
    esac
fi

port=8765
prev=""
for a in "$@"; do
    case "$prev" in --port) port=$a ;; esac
    case "$a" in --port=*) port=${a#--port=} ;; esac
    prev=$a
done

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 was not found. Run: sh $script_dir/install-ish.sh" >&2
    exit 1
fi

if [ -f "$root/kyora.pyz" ]; then
    set -- "$root/kyora.pyz" "$cmd" "$@"
elif [ -f "$root/kyora/__main__.py" ]; then
    PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}"
    export PYTHONPATH
    set -- -m kyora "$cmd" "$@"
else
    echo "Neither $root/kyora.pyz nor a kyora source tree was found." >&2
    exit 1
fi

if [ "$cmd" = serve ]; then
    echo "Open Safari at http://localhost:$port"
    echo "(keep iSH in the foreground; iOS pauses background apps)"
fi

exec python3 "$@"
