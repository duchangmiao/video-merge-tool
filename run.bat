@echo off
chcp 65001 >nul
cd /d %~dp0

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.9+ and check "Add Python to PATH".
    pause
    exit /b 1
)

python app.py
if errorlevel 1 pause
