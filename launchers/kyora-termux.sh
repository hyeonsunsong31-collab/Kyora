#!/bin/sh
# Start Kyora in Termux on Android.
#
# Usage:  sh launchers/kyora-termux.sh [--hub | serve | hub | selfcheck | bench | version] [args...]
#
#   (no command)  serve: start the local server and open the UI in the Android browser
#   --hub, hub    serve and expose the code engine on the LAN (Hub mode; needs Claude Code here)
#   other args    passed to Kyora unchanged, e.g.  sh launchers/kyora-termux.sh serve --port 9000
#
# Release layout: <root>/kyora.pyz and <root>/launchers/kyora-termux.sh -> "python <root>/kyora.pyz".
# Source checkout (no kyora.pyz): "python -m kyora" with <root> on PYTHONPATH.
# The script prints no keys or tokens itself and exits with Kyora's exit code.
set -eu

usage() {
    echo "Usage: sh launchers/kyora-termux.sh [--hub | serve | hub | selfcheck | bench | version] [args...]"
}

PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
# Termux has no /tmp; Python's tempfile and Kyora's runs use TMPDIR.
TMPDIR="$PREFIX/tmp"
export TMPDIR
mkdir -p "$TMPDIR"
KYORA_HOME="${KYORA_HOME:-$HOME/.kyora}"
export KYORA_HOME

script_dir=$(CDPATH='' cd "$(dirname "$0")" && pwd)
root=$(dirname "$script_dir")

cmd=serve
if [ $# -gt 0 ]; then
    case "$1" in
        --hub|hub) cmd=hub; shift ;;
        serve|selfcheck|bench|version) cmd=$1; shift ;;
        -h|--help|help) usage; exit 0 ;;
        -*) cmd=serve ;;
        *) echo "kyora-termux.sh: unknown command '$1'" >&2; usage >&2; exit 2 ;;
    esac
fi

# Port for the browser tab: Kyora's default unless --port is given on the command line.
port=8765
prev=""
for a in "$@"; do
    case "$prev" in --port) port=$a ;; esac
    case "$a" in --port=*) port=${a#--port=} ;; esac
    prev=$a
done

py=$(command -v python || command -v python3 || true)
if [ -z "$py" ]; then
    echo "Python was not found. Run: sh $script_dir/install-termux.sh" >&2
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

open_when_ready() {
    # Wait (up to 60 s) until the server answers, then open the UI in the Android browser.
    url=$1
    i=0
    while [ "$i" -lt 60 ]; do
        if "$py" -c 'import sys, urllib.request as u; u.build_opener(u.ProxyHandler({})).open(sys.argv[1], timeout=2)' \
            "$url/api/status" >/dev/null 2>&1; then
            termux-open-url "$url" >/dev/null 2>&1 || true
            return 0
        fi
        i=$((i + 1))
        sleep 1
    done
    return 0
}

if [ "$cmd" = serve ] || [ "$cmd" = hub ]; then
    if command -v termux-open-url >/dev/null 2>&1; then
        open_when_ready "http://127.0.0.1:$port" &
    else
        echo "Open http://127.0.0.1:$port in your browser (termux-open-url not found)."
    fi
fi

exec "$py" "$@"
