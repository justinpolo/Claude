@echo off
rem Downloads the latest Resale Sniper and copies it over this folder.
rem Your config.yaml, watches.yaml, sniper.db and logs are never touched.
setlocal
cd /d "%~dp0"
set "URL=https://github.com/justinpolo/Claude/archive/refs/heads/claude/sleepy-hopper-njfvqc.zip"
set "TMPD=%TEMP%\sniper_update"
if exist "%TMPD%" rmdir /s /q "%TMPD%"
mkdir "%TMPD%"
echo Downloading latest version...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -UseBasicParsing '%URL%' -OutFile '%TMPD%\s.zip'; Expand-Archive -Force '%TMPD%\s.zip' '%TMPD%\x'" || (echo Download failed & pause & exit /b 1)
for /d %%D in ("%TMPD%\x\*") do set "SRC=%%D"
robocopy "%SRC%" "%~dp0." /E /NFL /NDL /NJH /NJS /NP /XF config.yaml watches.yaml *.db *.db-wal *.db-shm *.log >nul
if errorlevel 8 (echo Copy failed & pause & exit /b 1)
rmdir /s /q "%TMPD%"
call .venv\Scripts\activate.bat
python -m pip install -q -r requirements.txt
echo Updated. Running connection check...
python -m sniper doctor
pause
