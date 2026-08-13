@echo off
cd /d "%~dp0"

REM 使用本机已安装的 Python（WorkBuddy managed runtime）
set "PY=C:\Users\llia\.workbuddy\binaries\python\versions\3.13.12\python.exe"

if not exist "%PY%" (
  echo [ERROR] Python not found at: %PY%
  echo Please check the path or install Python 3.8+
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
