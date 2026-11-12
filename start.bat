@echo off
title Resale Sniper
cd /d "%~dp0"
call .venv\Scripts\activate.bat
:loop
python -m sniper run
echo.
echo Sniper stopped (exit code %errorlevel%). Restarting in 10 seconds - close this window to quit.
timeout /t 10 /nobreak >nul
goto loop
