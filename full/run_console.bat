@echo off
rem Debug mode: logs in this window, Ctrl+C to quit
cd /d "%~dp0"
chcp 65001 >nul
".venv\Scripts\python.exe" "%~dp0main.py" --no-tray
pause
