@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  py -3 -m venv .venv
  if errorlevel 1 goto :failed
)
.venv\Scripts\python.exe -c "import pymupdf" >nul 2>&1
if errorlevel 1 (
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  if errorlevel 1 goto :failed
)
.venv\Scripts\python.exe app.py
goto :eof
:failed
echo 启动失败。请检查 Python 3.9+ 是否已安装，以及网络是否正常。
pause
