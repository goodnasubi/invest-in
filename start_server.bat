@echo off
echo.
echo ====================================
echo  Stock Watchlist - Data Server
echo ====================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found.
    echo Please install Python from https://www.python.org/
    pause
    exit /b 1
)

echo Installing yfinance (first time only)...
pip install yfinance --quiet --disable-pip-version-check
echo.

echo Server starting...
echo Browser will open automatically at http://localhost:5000
echo Close this window to stop the server.
echo.
python "%~dp0server.py"
pause
