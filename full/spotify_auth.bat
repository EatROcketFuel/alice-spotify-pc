@echo off
rem One-time Spotify login. Use "spotify_auth.bat manual" if the browser redirect fails.
cd /d "%~dp0"
chcp 65001 >nul
if /i "%~1"=="manual" (
    ".venv\Scripts\python.exe" "%~dp0main.py" --auth-manual
) else (
    ".venv\Scripts\python.exe" "%~dp0main.py" --auth
)
pause
