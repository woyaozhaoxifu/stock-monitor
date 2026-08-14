@echo off
cd /d "%~dp0"

REM Use WorkBuddy managed Python runtime
set "PY=C:\Users\llia\.workbuddy\binaries\python\versions\3.13.12\python.exe"

REM Disable WebView2 GPU acceleration (fixes crash on RDP/integrated GPU)
set "WEBVIEW2_ADDITIONAL_BROWSER_ARGS=--disable-gpu"
REM Force GBK output to match cmd.exe codepage (fix chinese mojibake)
set "PYTHONIOENCODING=gbk"

if not exist "%PY%" (
  echo [ERROR] Python not found at: %PY%
  echo Please check path or install Python 3.8+
  pause
  exit /b 1
)

echo ============================================
echo   StockBoard Starting...
echo ============================================
echo.

"%PY%" -u app.py >> "%~dp0stock-monitor.log" 2>&1
set "RC=%errorlevel%"

if not "%RC%"=="0" (
  echo.
  echo [ERROR] Exit code %RC%. Recent log:
  echo ----------------------------------------
  powershell -NoProfile -Command "Get-Content '%~dp0stock-monitor.log' -Tail 25"
  echo ----------------------------------------
  echo Check stock-monitor.log for details.
  pause
)
