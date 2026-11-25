@echo off
REM Double-click to open Pexels B-roll Finder on Windows.
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY goto nopython
%PY% -c "import tkinter" >nul 2>nul || goto nopython

if not exist ".venv\Scripts\python.exe" (
  echo First run: setting up, takes about 30 seconds...
  %PY% -m venv .venv || goto nopython
)
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
start "" ".venv\Scripts\pythonw.exe" gui.py
exit /b

:nopython
echo.
echo Python 3 was not found. Install it from python.org and tick "Add python.exe to PATH".
start https://www.python.org/downloads/windows/
pause
