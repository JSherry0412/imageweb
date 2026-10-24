@echo off
cd /d "%~dp0"
python -c "import flask, requests" >nul 2>nul
if errorlevel 1 (
  echo Installing Image Atelier dependencies...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Dependency installation failed.
    pause
    exit /b 1
  )
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-server.ps1"
if errorlevel 1 pause
