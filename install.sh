#!/usr/bin/env bash
# Install Toony into its own virtualenv and register the user service.
# Usage: ./install.sh [--extras "openai,local,piper"]
set -euo pipefail

EXTRAS="openai,local,piper"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --extras) EXTRAS="$2"; shift 2 ;;
        --all)    EXTRAS="claude,openai,local,wake,vad,piper"; shift ;;
        -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${XDG_DATA_HOME:-$HOME/.local/share}/toony/venv"
BIN="$HOME/.local/bin"

echo "==> checking system packages"
missing=()
for cmd in python3 pipewire; do
    command -v "$cmd" >/dev/null || missing+=("$cmd")
done
if [[ ${#missing[@]} -gt 0 ]]; then
    echo "    missing: ${missing[*]}" >&2
fi

# Toony shells out to these. None are fatal; each one just enables a tool.
suggest=()
for cmd in wpctl playerctl spectacle wl-copy notify-send brightnessctl kdotool; do
    command -v "$cmd" >/dev/null || suggest+=("$cmd")
done
if [[ ${#suggest[@]} -gt 0 ]]; then
    echo "    optional commands not found: ${suggest[*]}"
    echo "    on Fedora KDE:  sudo dnf install wireplumber playerctl spectacle wl-clipboard libnotify brightnessctl"
fi

echo "==> creating the virtualenv at $VENV"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip wheel >/dev/null

echo "==> installing toony[$EXTRAS]"
"$VENV/bin/pip" install -e "$HERE[$EXTRAS]"

mkdir -p "$BIN"
ln -sf "$VENV/bin/toony" "$BIN/toony"
echo "==> linked $BIN/toony"
case ":$PATH:" in
    *":$BIN:"*) ;;
    *) echo "    NOTE: $BIN is not on your PATH — add it to ~/.bashrc" ;;
esac

echo "==> downloading a Piper voice"
"$BIN/toony" voices install en_US-amy-medium || \
    echo "    (skipped — run 'toony voices install en_US-amy-medium' later)"

echo "==> installing the user service and the push-to-talk hotkey"
"$BIN/toony" install

echo
echo "Done. Next:"
echo "  toony doctor                     check what is missing"
echo "  toony ask 'what time is it'      talk to it without the microphone"
echo "  press Meta+Space                 push to talk"
