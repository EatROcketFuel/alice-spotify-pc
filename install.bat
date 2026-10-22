@echo off
setlocal
cd /d "%~dp0"
echo === Alice PC Controller: install ===

where py >nul 2>nul
if %errorlevel%==0 (
    set "PY=py -3"
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] Python not found. Install Python 3.12 from https://www.python.org/downloads/
        echo         and tick "Add python.exe to PATH" during setup.
        pause
        exit /b 1
    )
    set "PY=python"
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create the virtual environment.
        pause
        exit /b 1
    )
)

echo Installing dependencies ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] pip install failed.
    pause
    exit /b 1
)

echo.
echo Done. Next steps:
echo   1. settings.bat             - settings window: WQTT, speaker/headphones, autostart
echo   2. run_console.bat          - test in a console window
echo   3. run.bat                  - start in the system tray (tray menu - Settings)
pause
