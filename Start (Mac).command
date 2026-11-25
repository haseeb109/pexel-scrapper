#!/bin/bash
# Double-click to open Pexels B-roll Finder on macOS.
cd "$(dirname "$0")" || exit 1
export TK_SILENCE_DEPRECATION=1

# Prefer python.org / Homebrew Python (modern Tk) over Apple's old system Python 3.9.
PY=""
for c in /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
         python3.13 python3.12 python3.11 python3.10 \
         /Library/Frameworks/Python.framework/Versions/Current/bin/python3 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c "import tkinter" >/dev/null 2>&1; then PY="$c"; break; fi
done

if [ -z "$PY" ]; then
  osascript -e 'display alert "Python is needed" message "Install Python from python.org (it includes the window toolkit this app uses), then double-click Start again." buttons {"Open python.org"}' >/dev/null
  open "https://www.python.org/downloads/macos/"
  exit 1
fi

WANT="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
HAVE=""
[ -x ".venv/bin/python" ] && HAVE="$(.venv/bin/python -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"
if [ "$HAVE" != "$WANT" ]; then
  [ -n "$HAVE" ] && echo "Switching from Python $HAVE to $WANT…"
  rm -rf .venv
  echo "Setting up (one time, about a minute)…"
  "$PY" -m venv .venv || exit 1
  .venv/bin/python -m pip install -q --disable-pip-version-check --upgrade pip >/dev/null 2>&1
fi

REQ_HASH="$(shasum requirements.txt | cut -d' ' -f1)"
if [ "$(cat .venv/.req_hash 2>/dev/null)" != "$REQ_HASH" ]; then
  echo "Installing packages…"
  if .venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt; then
    echo "$REQ_HASH" > .venv/.req_hash
  else
    echo "Package install had a problem (are you online?). Trying to start anyway…"
  fi
fi

echo "Opening Pexels B-roll Finder… (keep this window open; you can minimise it)"
.venv/bin/python gui.py
