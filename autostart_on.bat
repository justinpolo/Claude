@echo off
rem Start Resale Sniper (minimized) every time you log in to Windows.
set STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
(
  echo @echo off
  echo start "Resale Sniper" /min cmd /c "%~dp0start.bat"
) > "%STARTUP%\ResaleSniper.bat"
echo Autostart enabled: %STARTUP%\ResaleSniper.bat
echo Tip: in Windows power settings set "Sleep" to Never while plugged in, or alerts stop when the PC sleeps.
pause
