@echo off
rem Settings window (MQTT, audio devices, Spotify, autostart)
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" "%~dp0main.py" --settings
