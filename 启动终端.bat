@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [WTMonitorTerminal] starting...  (keep this window open; errors print here)
".venv\Scripts\python.exe" main.py
if %errorlevel% neq 0 (
  echo.
  echo [exited with error code %errorlevel%]
  pause
)
