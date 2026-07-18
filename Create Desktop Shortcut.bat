@echo off
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws = New-Object -ComObject WScript.Shell;" ^
  "$s = $ws.CreateShortcut((Join-Path $ws.SpecialFolders('Desktop') 'Blink Live View.lnk'));" ^
  "$s.TargetPath = (Join-Path (Get-Location) 'Run.bat');" ^
  "$s.WorkingDirectory = (Get-Location).Path;" ^
  "$s.IconLocation = (Join-Path (Get-Location) 'blink.ico');" ^
  "$s.Description = 'Open the Blink Live View window';" ^
  "$s.Save()"

if errorlevel 1 (
  echo Could not create the shortcut. See the error above.
) else (
  echo Shortcut created on your Desktop: "Blink Live View"
)
pause
endlocal
