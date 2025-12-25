param(
  [string]$ProjectDir = (Split-Path -Parent $MyInvocation.MyCommand.Path),
  [string]$PythonExe = "python"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Set-Location $ProjectDir

# 说明：
# - 建议在 Windows 任务计划程序里，把本脚本设置为每天 23:00 运行
# - 如需上传到 R2，请在任务里配置环境变量：
#   S3_ENDPOINT_URL / S3_BUCKET_NAME / S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY / S3_REGION
# - 或者在运行前手动设置：$env:S3_BUCKET_NAME="..."

& $PythonExe -m trendradar

