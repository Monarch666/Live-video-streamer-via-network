@echo off
REM run_app.bat — Launch the Live Video Relay app on Windows
REM Double-click this to start the app, or run from Command Prompt.

cd /d "%~dp0"
python app.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo App exited with an error. Run install.bat first if you haven't.
    pause
)
