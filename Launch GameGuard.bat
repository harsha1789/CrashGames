@echo off
REM Double-click this to start the GameGuard dashboard and open it in your browser.
REM Run "Setup (first time).bat" once before using this, if you haven't already.
setlocal
cd /d "%~dp0"

set GAMEGUARD_PORT=5000
set GAMEGUARD_RELOADER=0
set URL=http://127.0.0.1:%GAMEGUARD_PORT%/

echo Checking if GameGuard is already running on port %GAMEGUARD_PORT% ...
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri '%URL%' -UseBasicParsing -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }" >nul 2>nul
if %errorlevel%==0 (
    echo Already running - opening it in your browser.
    start "" "%URL%"
    goto :end
)

echo Starting GameGuard dashboard...
echo (A window titled "GameGuard Server" will open minimized - leave it running.
echo  Use "Stop GameGuard.bat" to shut it down, or just close that window.)
start "GameGuard Server - leave this open" /min cmd /c "set GAMEGUARD_PORT=%GAMEGUARD_PORT%&& set GAMEGUARD_RELOADER=0&& python app.py"

echo Waiting for it to come up...
powershell -NoProfile -Command "$u='%URL%'; for($i=0;$i -lt 60;$i++){ try { Invoke-WebRequest -Uri $u -UseBasicParsing -TimeoutSec 2 | Out-Null; exit 0 } catch { Start-Sleep -Milliseconds 500 } }; exit 1"
if errorlevel 1 (
    echo.
    echo [!] The server did not respond after 30 seconds.
    echo     Check the "GameGuard Server" window for an error message.
    pause
    goto :end
)

start "" "%URL%"

:end
endlocal
