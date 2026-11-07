@echo off
REM SmartPrint - 启动脚本（cmd）
REM 使用已有的 conda 环境 py313 运行。
setlocal

set "PYTHON_EXE=D:\Program\Python\anaconda3\envs\py313\python.exe"

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Python not found: %PYTHON_EXE%
    echo Please check the conda env py313, or edit this script.
    pause
    exit /b 1
)

cd /d "%~dp0"
"%PYTHON_EXE%" -m app.main
if errorlevel 1 pause
endlocal
