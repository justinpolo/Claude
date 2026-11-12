@echo off
setlocal
cd /d "%~dp0"
echo === Resale Sniper installer ===
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% --version >nul 2>nul || (
  echo Python 3.10+ is required. Install it from https://www.python.org/downloads/
  echo IMPORTANT: tick "Add python.exe to PATH" in the installer, then run this again.
  pause & exit /b 1
)
if not exist .venv (
  echo Creating virtual environment...
  %PY% -m venv .venv || (echo Failed to create venv & pause & exit /b 1)
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt || (echo pip install failed & pause & exit /b 1)
if not exist config.yaml copy config.example.yaml config.yaml >nul && echo Created config.yaml
if not exist watches.yaml copy watches.example.yaml watches.yaml >nul && echo Created watches.yaml
echo.
echo Checking connection to Depop and Vinted...
python -m sniper doctor
echo.
echo Done. Next steps:
echo   1. Edit watches.yaml (what to look for)
echo   2. Set up phone alerts - see README.md "Telegram" (2 minutes)
echo   3. Double-click start.bat
pause
