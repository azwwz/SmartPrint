# SmartPrint - 启动脚本（PowerShell）
# 使用已有的 conda 环境 py313 运行，不需要手动 conda activate。
$ErrorActionPreference = "Stop"

$PythonExe = "D:\Program\Python\anaconda3\envs\py313\python.exe"

if (-not (Test-Path $PythonExe)) {
    Write-Host "找不到 Python 解释器：$PythonExe" -ForegroundColor Red
    Write-Host "请确认 conda 环境 py313 是否存在，或在脚本中修改 `$PythonExe。" -ForegroundColor Yellow
    exit 1
}

Set-Location -Path $PSScriptRoot
& $PythonExe -m app.main
exit $LASTEXITCODE
