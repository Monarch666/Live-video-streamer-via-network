@echo off
echo Installing dependencies from requirements.txt...
pip install -r requirements.txt
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ERROR: Failed to install requirements.
    pause
    exit /b %ERRORLEVEL%
)
echo.
echo Requirements installed successfully!
pause
