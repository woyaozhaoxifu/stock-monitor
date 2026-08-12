@echo off
chcp 65001 >nul
title StockBoard Public Share
cd /d D:\stock-monitor

echo ============================================
echo   Starting dashboard server + public tunnel
echo ============================================

REM 1) start backend server (headless) if not already on 8787
set NO_WINDOW=1
netstat -ano | find ":8787" | find "LISTENING" >nul
if errorlevel 1 (
  echo [1/2] starting dashboard server ...
  start "stockboard-server" /min C:\Users\llia\.workbuddy\binaries\python\versions\3.13.12\python.exe app.py
  timeout /t 4 >nul
) else (
  echo [1/2] server already running on 8787, skip
)

REM 2) start public tunnel in a VISIBLE window (the public URL shows there)
echo [2/2] starting public tunnel ... see the "PUBLIC-TUNNEL" window for the URL
start "PUBLIC-TUNNEL" D:\tools\cloudflared.exe tunnel --url http://localhost:8787 --no-autoupdate

echo.
echo Done. Open the PUBLIC-TUNNEL window, copy the https://....trycloudflare.com URL and share it.
echo Keep both windows open while sharing. Close them to stop sharing.
pause
