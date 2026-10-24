#!/bin/sh
# Install Kyora's prerequisites in iSH on iOS (Alpine Linux, busybox ash): Python 3 and Git.
#
# Usage:  sh launchers/install-ish.sh
#
# Idempotent: apk skips packages that are already installed. The script never reads or
# prints keys or tokens.
set -eu

for arg in "$@"; do
    case "$arg" in
        -h|--help) echo "Usage: sh launchers/install-ish.sh"; exit 0 ;;
        *) echo "install-ish.sh: unknown option '$arg'" >&2; exit 2 ;;
    esac
done

if ! command -v apk >/dev/null 2>&1; then
    echo "apk was not found: this installer is for iSH (Alpine Linux) on iOS." >&2
    exit 1
fi

echo "==> apk update"
apk update
echo "==> apk add python3 git"
apk add python3 git

echo "==> Verifying"
python3 --version
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "Kyora needs Python 3.10 or newer. Point /etc/apk/repositories at a newer Alpine release," >&2
    echo "run 'apk update && apk upgrade', then re-run this script." >&2
    exit 1
fi
git --version

script_dir=$(CDPATH='' cd "$(dirname "$0")" && pwd)
cat <<'EOF'

Notes for iSH (iOS):
  * iSH emulates a 32-bit x86 CPU in software, so everything is slow. The first start takes a
    while; Kyora Bench is not recommended here.
  * Node.js does not run usefully under iSH, so Claude Code cannot run on the phone. BUILD and FIX
    go through a Kyora Hub: on your PC run "kyora.ps1 hub" (or "kyora hub"), then scan the QR code
    it prints with the iPhone camera / open the link in Safari to link this phone. Without a Hub,
    BUILD runs are reported as BLOCKED (never faked).
  * iOS pauses iSH a few seconds after Safari comes to the front, and a paused iSH cannot answer
    Safari. Kyora keeps iSH running on its own while it serves: iOS asks once for location access
    for iSH - choose "Allow While Using App" (Kyora keeps no location; it only holds iSH's location
    stream open). Open http://127.0.0.1:8765 in Safari once Kyora prints "Open:".
  * Browser tests run in Safari through Kyora's test harness page; the UI opens it for you.
EOF
echo ""
echo "Next steps:"
echo "    sh $script_dir/kyora-ish.sh selfcheck"
echo "    sh $script_dir/kyora-ish.sh            (then open http://127.0.0.1:8765 in Safari)"
