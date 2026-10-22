@echo off
rem Start in the background (tray icon, no console window)
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" "%~dp0main.py"
