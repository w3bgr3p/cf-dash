@echo off
chcp 65001 >nul
title CF Inventory — Install

echo.
echo  ╔══════════════════════════════════╗
echo  ║     CF Inventory — Install       ║
echo  ╚══════════════════════════════════╝
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found.
    echo.
    echo  Install Python 3.10+ from https://python.org/downloads
    echo  Make sure to check "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo  Python %PYVER% found.
echo.

:: Install dependencies
echo  Installing dependencies...
echo.
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt --quiet

if errorlevel 1 (
    echo.
    echo  [ERROR] Failed to install dependencies.
    echo  Try running as Administrator.
    pause
    exit /b 1
)

echo.
echo  Done. Run run.bat to start.
echo.
pause
