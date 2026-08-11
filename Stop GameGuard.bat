@echo off
REM Double-click to cleanly shut down a GameGuard dashboard started by "Launch GameGuard.bat".
setlocal
set GAMEGUARD_PORT=5000

echo Stopping any GameGuard server on port %GAMEGUARD_PORT% ...
powershell -NoProfile -Command ^
  "$conns = Get-NetTCPConnection -LocalPort %GAMEGUARD_PORT% -State Listen -ErrorAction SilentlyContinue; if ($conns) { $conns | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { try { Stop-Process -Id $_ -Force; Write-Host \"Stopped process $_\" } catch {} } } else { Write-Host 'Nothing was running on that port.' }"

pause
endlocal
