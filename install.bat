@echo off
REM install.bat — One-time setup for the Live Video Relay app (Windows)
REM Run this ONCE before launching app.py
REM
REM Usage: Double-click install.bat, OR run from a Command Prompt / PowerShell

echo ===================================================
echo  Live Video Relay -- Windows Installer
echo ===================================================
echo.

REM Check Python is available
where python >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Python not found.
    echo.
    echo Install Python 3.10+ from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    echo tkinter is included automatically with the official Python installer.
    pause
    exit /b 1
)

echo Python found:
python --version
echo.

REM Tkinter check
python -c "import tkinter" 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo WARNING: tkinter not found.
    echo.
    echo If you installed Python from python.org, re-run the installer and
    echo ensure "tcl/tk and IDLE" is selected in Optional Features.
    echo.
    echo If you installed from the Microsoft Store, uninstall it and use the
    echo official installer from https://www.python.org/downloads/ instead.
    pause
    exit /b 1
)
echo tkinter: OK

REM Install Python packages
echo.
echo Installing Python packages...
pip install --user opencv-python Pillow

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ERROR: pip install failed. Try running as Administrator or check
    echo your internet connection.
    pause
    exit /b 1
)

echo.
echo NOTE: MediaMTX (for server mode) will be auto-downloaded on first use.
echo       No manual installation is required.

echo.
echo ===================================================
echo  Installation complete!
echo ===================================================
echo.
echo To launch the app, open a terminal in this folder and run:
echo     python app.py
echo.
echo Or double-click run_app.bat (if present).
pause
