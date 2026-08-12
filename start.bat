@echo off
cd /d "%~dp0"

REM 优先使用系统 PATH 中的 python，找不到再回退到已知路径
set "PY=python"
where python >nul 2>nul
if errorlevel 1 (
  set "PY=C:\Users\llia\.workbuddy\binaries\python\versions\3.13.12\python.exe"
)
if not exist "%PY%" (
  echo [ERROR] Python not found. Please install Python 3.8+ and add it to PATH.
  pause
  exit /b 1
)

echo ============================================
echo   StockBoard Starting...
echo ============================================
echo.

"%PY%" app.py
if errorlevel 1 (
  echo.
  echo [ERROR] Startup failed. Press any key.
  pause
)
