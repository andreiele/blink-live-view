@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  start "" ".venv\Scripts\pythonw.exe" live.py
) else (
  start "" pythonw live.py
)
endlocal
