@echo off
REM One-time environment setup for a new QA machine. Run this ONCE before using
REM "Launch GameGuard.bat". Safe to re-run later (e.g. after a requirements.txt update).
setlocal
cd /d "%~dp0"

echo ============================================================
echo  GameGuard - first-time setup
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [!] Python was not found on PATH.
    echo     Install Python 3.11+ from https://www.python.org/downloads/
    echo     During install, tick "Add python.exe to PATH".
    pause
    exit /b 1
)

echo [1/3] Installing Python packages from requirements.txt ...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [!] pip install failed - see the errors above.
    pause
    exit /b 1
)

echo.
echo [2/3] Installing the Playwright browser (Chromium) ...
python -m playwright install chromium
if errorlevel 1 (
    echo [!] Playwright browser install failed - see the errors above.
    pause
    exit /b 1
)

echo.
echo [3/3] Checking for api_keys.json (Gemini vision key) ...
if not exist "api_keys.json" (
    echo [!] api_keys.json is missing. Copy api_keys.example.json to api_keys.json
    echo     and fill in a real Gemini API key before running the dashboard.
) else (
    echo     Found.
)

echo.
echo ============================================================
echo  Setup complete. Use "Launch GameGuard.bat" to start the dashboard.
echo ============================================================
pause
endlocal
