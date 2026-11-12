@echo off
rem Run any sniper command, e.g.:  sniper.bat search "nike dunk" --max 60
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python -m sniper %*
