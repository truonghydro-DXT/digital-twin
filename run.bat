@echo off
title Digital twin
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" (
  echo Khong thay venv\Scripts\python.exe
  pause
  exit /b 1
)
"venv\Scripts\python.exe" -u "view-output.py" "output-3d" --host 0.0.0.0 --port 8080
pause
