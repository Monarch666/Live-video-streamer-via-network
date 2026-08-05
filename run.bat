@echo off
title Live Video Relay Launcher
cd /d "%~dp0"

echo ======================================================
echo              Live Video Relay Launcher                
echo ======================================================
echo.

REM Check Python installation
where python >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python is not found on your system!
    echo Please install Python 3.10+ from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

REM Check and auto-install required dependencies
python -c "import cv2, PIL" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [INFO] Installing required Python dependencies (opencv-python, Pillow)...
    pip install opencv-python Pillow
    if %ERRORLEVEL% NEQ 0 (
        echo [ERROR] Failed to install dependencies. Please check your internet connection.
        pause
        exit /b 1
    )
    echo [OK] Dependencies installed successfully.
    echo.
)

REM Launch GUI Application
echo Launching Live Video Relay GUI...
echo.

python app.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Application exited with error code %ERRORLEVEL%.
    pause
)
