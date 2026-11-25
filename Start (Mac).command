#!/bin/bash
# Double-click to open Pexels B-roll Finder on macOS.
cd "$(dirname "$0")" || exit 1

PY=""
for c in python3.13 python3.12 python3.11 python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c "import tkinter" >/dev/null 2>&1; then PY="$c"; break; fi
done

if [ -z "$PY" ]; then
  osascript -e 'display alert "Python is needed" message "Install Python from python.org (it includes the window toolkit this app uses), then double-click Start again." buttons {"Open python.org"}' >/dev/null
  open "https://www.python.org/downloads/macos/"
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "First run: setting up (takes ~30 seconds)…"
  "$PY" -m venv .venv || exit 1
fi
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt

echo "Opening Pexels B-roll Finder… (you can minimise this Terminal window)"
.venv/bin/python gui.py
