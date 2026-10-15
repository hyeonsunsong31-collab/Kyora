#!/bin/sh
# Install Kyora's prerequisites in Termux on Android: Python, Git and Node.js LTS.
#
# Usage:  sh launchers/install-termux.sh [--with-claude]
#
#   --with-claude   also try to install Claude Code with npm and check that it runs.
#                   Claude Code has no official Android build, so this usually fails; the
#                   script then explains the supported route (Hub mode from a PC).
#
# Idempotent: pkg skips packages that are already installed, and Node.js is only installed
# when no `node` is on PATH (so an existing nodejs package is not replaced). The script never
# reads or prints keys or tokens.
set -eu

usage() {
    echo "Usage: sh launchers/install-termux.sh [--with-claude]"
    echo "  --with-claude  also try 'npm install -g @anthropic-ai/claude-code' (no official Android build)"
}

with_claude=0
for arg in "$@"; do
    case "$arg" in
        --with-claude) with_claude=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "install-termux.sh: unknown option '$arg'" >&2; usage >&2; exit 2 ;;
    esac
done

if ! command -v pkg >/dev/null 2>&1; then
    echo "pkg was not found: this installer is for Termux on Android." >&2
    echo "On a PC use launchers/install.ps1 (Windows); on iOS use launchers/install-ish.sh (iSH)." >&2
    exit 1
fi

echo "==> pkg update -y"
pkg update -y

if command -v node >/dev/null 2>&1; then
    echo "==> pkg install -y python git   (Node.js already present: $(node --version))"
    pkg install -y python git
else
    echo "==> pkg install -y python git nodejs-lts"
    pkg install -y python git nodejs-lts
fi

echo "==> Verifying"
python --version
if ! python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "Kyora needs Python 3.10 or newer; run 'pkg upgrade' and try again." >&2
    exit 1
fi
git --version
node --version

claude_ok=0
if [ "$with_claude" -eq 1 ]; then
    echo "==> Trying Claude Code: npm install -g @anthropic-ai/claude-code"
    if npm install -g @anthropic-ai/claude-code && claude --version; then
        claude_ok=1
        echo "Claude Code runs in this Termux. Log in with your Claude subscription (no API key needed):"
        echo "    claude auth login"
    else
        cat <<'EOF'

Claude Code did not install or does not run here. This is expected:
  * Claude Code has no official Android build. Its native builds cover macOS, Linux and
    Windows; Termux on Android (Bionic libc) is not one of them.
  * Supported route: Hub mode. On a PC with Claude Code, run "kyora.ps1 hub" (or "kyora hub"),
    then open the Hub link / scan its QR code on this phone. BUILD and FIX jobs run on the PC;
    models, tests, preview, slides and downloads run here.
  * A community "glibc-runner" route for Termux exists, but it is unofficial and unsupported
    by Kyora.
  * Advanced, user-managed: Claude Code inside a proot-distro Ubuntu, wired up through
    Settings -> claude_code.command (a command that runs claude inside the distro; Kyora's
    workspace directory must be visible at the same path there). Not tested by Kyora.
Without a code engine Kyora reports BUILD as BLOCKED; it never fakes a build.
EOF
    fi
fi

script_dir=$(CDPATH='' cd "$(dirname "$0")" && pwd)
echo ""
echo "Prerequisites are ready."
if [ "$claude_ok" -eq 0 ]; then
    echo "Code engine: link a Kyora Hub (Settings -> Hub) or BUILD runs will be BLOCKED."
fi
echo "Next steps:"
echo "    sh $script_dir/kyora-termux.sh selfcheck"
echo "    sh $script_dir/kyora-termux.sh            (starts Kyora and opens http://127.0.0.1:8765)"
