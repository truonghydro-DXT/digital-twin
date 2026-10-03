@echo off
title DEM 3D
set "ROOT_DIR=%~dp0"
cd /d "%ROOT_DIR%"

set "PY="
if exist "%ROOT_DIR%venv\Scripts\python.exe" set "PY=%ROOT_DIR%venv\Scripts\python.exe"
if not defined PY if exist "%ROOT_DIR%..\venv\Scripts\python.exe" set "PY=%ROOT_DIR%..\venv\Scripts\python.exe"
if not defined PY if exist "%ROOT_DIR%..\..\venv\Scripts\python.exe" set "PY=%ROOT_DIR%..\..\venv\Scripts\python.exe"
if not defined PY set "PY=py"

echo.
echo ================================
echo   DEM 3D — ung dung doc lap
echo ================================
echo.

"%PY%" app.py
if errorlevel 1 (
    echo Loi khi chay app.py
    pause
    exit /b
)
pause
