@echo off
setlocal
cd /d "%~dp0"

echo Blink Live View - Setup
echo =======================
echo.

where ffplay >nul 2>nul
if errorlevel 1 (
  echo WARNING: ffplay was not found on your PATH.
  echo Blink Live View needs FFmpeg to actually play the video stream.
  echo Install it from https://ffmpeg.org/download.html or run: winget install ffmpeg
  echo You can finish this setup ^(sign-in, camera pick^) without it, but Run.bat
  echo will fail to show video until ffplay is installed and on your PATH.
  echo.
)

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
