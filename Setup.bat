@echo off
setlocal
cd /d "%~dp0"

echo Blink Live View - Setup
echo =======================
echo.

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  where py >nul 2>nul
  if not errorlevel 1 (
    py -3 -m venv .venv
  ) else (
    python -m venv .venv
  )
  if not exist ".venv\Scripts\python.exe" (
    echo.
    echo Could not create a virtual environment. Make sure Python 3 is installed
    echo and available as "py" or "python", then run this again.
    pause
    exit /b 1
  )
)

echo Installing dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo Failed to install dependencies. See the errors above.
  pause
  exit /b 1
)

echo.
echo Signing in and picking a camera...
echo.
".venv\Scripts\python.exe" first_run.py

echo.
pause
endlocal
