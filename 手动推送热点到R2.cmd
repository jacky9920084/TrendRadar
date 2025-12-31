@echo off
setlocal

REM Hotspot Spark / TrendRadar one-shot runner (manual).
REM Runs: crawl -> export ai_hotspots -> upload to R2 (ai-hotspots/YYYY/MM/DD/hotspots.txt)

set "SCRIPT=%~dp0run-daily-23-r2.ps1"
if not exist "%SCRIPT%" (
  echo ERROR: missing script: %SCRIPT%
  exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%"
endlocal
