@echo off
rem Builds UniView - Unity Asset Viewer into the Builds folder:
rem   Builds\GitHub   - clean source copy, ready to push to a GitHub repo
rem   Builds\Release  - standalone .exe folder + .zip (no Python needed)
rem Usage: build.bat [github] [release] [--test-game "path\to\game"]
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    echo Python launcher "py" not found. Install Python from python.org first.
    pause
    exit /b 1
)

if not exist "Builds" mkdir "Builds"

echo Installing/updating requirements...
py -m pip install --disable-pip-version-check -q -r requirements.txt pyinstaller
if errorlevel 1 (
    echo pip install failed.
    pause
    exit /b 1
)

py build.py %*
set RESULT=%ERRORLEVEL%
if %RESULT% neq 0 (
    echo.
    echo BUILD FAILED - see the messages above.
) else (
    echo.
    echo Build finished. Output is in "%~dp0Builds"
)
pause
exit /b %RESULT%
