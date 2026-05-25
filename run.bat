@echo off
chcp 65001 >nul
title CF Inventory

echo.
echo  ╔══════════════════════════════════╗
echo  ║       CF Inventory — Run         ║
echo  ╚══════════════════════════════════╝
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found. Run install.bat first.
    pause
    exit /b 1
)

:: Check main.py exists
if not exist main.py (
    echo  [ERROR] main.py not found.
    echo  Make sure run.bat is in the same folder as main.py.
    pause
    exit /b 1
)

:: Create .env if missing
if not exist .env (
    echo  No .env file found — creating empty one.
    echo  You can set CF_TOKEN=your_token inside it to skip the login screen.
    echo.
    echo # Cloudflare Inventory config > .env
    echo # CF_TOKEN=your_cloudflare_api_token >> .env
    echo # PORT=19232 >> .env
)

:: Read PORT from .env if set
set PORT=19232
for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
    if /i "%%a"=="PORT" set PORT=%%b
)

echo  Starting server on http://localhost:%PORT%
echo  Press Ctrl+C to stop.
echo.

:: Open browser after short delay (background)
start /b cmd /c "timeout /t 2 >nul && start http://localhost:%PORT%"

:: Start server
python main.py

echo.
echo  Server stopped.
pause
