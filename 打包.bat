@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [打包] 正在生成 dist\WTMonitorTerminal\WTMonitorTerminal.exe ...
echo (第一次会慢一些; 出问题就看下面的报错)
echo.
".venv\Scripts\python.exe" -m PyInstaller wt_terminal.spec --noconfirm --clean
if %errorlevel% neq 0 (
  echo.
  echo [打包失败] 上面有报错。
  pause
  exit /b 1
)
echo.
echo [完成] dist\WTMonitorTerminal\WTMonitorTerminal.exe
echo       双击它即可运行。首次启动会生成 config.yaml，运行数据保存在 data 文件夹。
pause
