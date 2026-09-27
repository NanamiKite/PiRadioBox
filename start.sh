#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "$(realpath -- "${BASH_SOURCE[0]}")")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 is missing. On Raspberry Pi OS, install it with:" >&2
    echo "  sudo apt update && sudo apt install python3 python3-venv python3-full vlc fonts-wqy-microhei" >&2
    exit 1
fi

if ! python3 -c 'import tkinter' >/dev/null 2>&1 && [[ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
    echo "No desktop display is available. Start this script from a graphical Raspberry Pi desktop." >&2
    echo "For auto-start at login, add this project's start.sh to your desktop's autostart settings." >&2
    exit 1
fi

if ! command -v vlc >/dev/null 2>&1; then
    echo "VLC is missing. Install it with: sudo apt install vlc" >&2
    exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
    echo "Setting up the app's private Python environment..."
    python3 -m venv .venv || {
        echo "Could not create a virtual environment. Install python3-venv and python3-full first." >&2
        echo "  sudo apt install python3-venv python3-full" >&2
        exit 1
    }
fi

echo "Checking Python packages..."
.venv/bin/python -m pip install -r requirements.txt
echo "Starting PiRadioBox..."
exec .venv/bin/python main.py
