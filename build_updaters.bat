@echo off
REM ============================================================
REM Compile the updater_cli.py into updater_cli.exe (standalone).
REM Drop the produced .exe next to UniversalAudioStudio.exe.
REM
REM   Usage:  build_updaters.bat
REM
REM Requires PyInstaller:  pip install pyinstaller
REM ============================================================
pushd "%~dp0"

set "PY="
python -m PyInstaller --version >nul 2>nul
if not errorlevel 1 ( set "PY=python -m PyInstaller" )
if not defined PY (
    echo ERROR: PyInstaller not found.  Run:  pip install pyinstaller
    exit /b 1
)

echo Building updater_cli.exe ...
%PY% --onefile --noconsole --name "updater_cli" updater_cli.py
if errorlevel 1 (
    echo ERROR: updater_cli build failed.
    exit /b 1
)

echo.
echo Done.  dist\updater_cli.exe is picked up automatically by the next
echo project build (UniversalAudioStudio.spec bundles it with the app).
popd