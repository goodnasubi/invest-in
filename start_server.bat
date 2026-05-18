@echo off
echo.
echo ====================================
echo  Stock Watchlist - Data Server
echo ====================================
echo.

uv --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] uv not found.
    echo Please install uv from https://docs.astral.sh/uv/
    echo   winget install --id=astral-sh.uv
    pause
    exit /b 1
)

cd /d "%~dp0"

echo Syncing dependencies (first time only)...
uv sync --quiet
echo.

echo Server starting...
echo Browser will open automatically at http://localhost:5000
echo Close this window to stop the server.
echo.
uv run python server.py
pause
