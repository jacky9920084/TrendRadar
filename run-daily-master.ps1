param(
  [string]$ProjectDir = "E:\\cursor\\TrendRadar",
  [string]$Date = "",
  [int]$WebConcurrency = 2,
  [int]$DouyinConcurrency = 2
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

function Get-ShanghaiIsoDate {
  $tz = $null
  try { $tz = [TimeZoneInfo]::FindSystemTimeZoneById("China Standard Time") } catch { $tz = $null }
  if (-not $tz) { return (Get-Date).ToString("yyyy-MM-dd") }
  $now = [TimeZoneInfo]::ConvertTime([DateTime]::UtcNow, $tz)
  return $now.ToString("yyyy-MM-dd")
}

function Assert-IsoDate {
  param([Parameter(Mandatory = $true)][string]$DateStr)
  if ($DateStr -notmatch '^\d{4}-\d{2}-\d{2}$') { throw "Invalid date format: $DateStr (expected YYYY-MM-DD)." }
}

function Write-Log {
  param(
    [Parameter(Mandatory = $true)][string]$LogPath,
    [Parameter(Mandatory = $true)][string]$Message
  )
  $line = ("[{0}] {1}" -f (Get-Date).ToString("yyyy-MM-dd HH:mm:ss"), $Message)
  [IO.File]::AppendAllText($LogPath, $line + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
}

function Log {
  param([Parameter(Mandatory = $true)][string]$Msg)
  Write-Log -LogPath $script:LogPath -Message $Msg
}

function Is-Port-Open {
  param([Parameter(Mandatory = $true)][string]$Host, [Parameter(Mandatory = $true)][int]$Port)
  $client = New-Object System.Net.Sockets.TcpClient
  try {
    $iar = $client.BeginConnect($Host, $Port, $null, $null)
    $ok = $iar.AsyncWaitHandle.WaitOne(800)
    return ($ok -and $client.Connected)
  } finally {
    try { $client.Close() } catch {}
  }
}

Set-Location $ProjectDir

$effectiveDate = if ($Date) { $Date } elseif ($env:DATE) { $env:DATE } else { Get-ShanghaiIsoDate }
Assert-IsoDate -DateStr $effectiveDate

$outDir = Join-Path $ProjectDir "output"
if (-not (Test-Path -LiteralPath $outDir)) { New-Item -ItemType Directory -Path $outDir | Out-Null }
$script:LogPath = Join-Path $outDir ("master-{0}.log" -f $effectiveDate)

Log ("Start master; date={0}; web_concurrency={1}; douyin_concurrency={2}" -f $effectiveDate, $WebConcurrency, $DouyinConcurrency)

try {
  Log "step1: 生成主清单并上传R2（并替换抖音为新源）"
  & ".\\run-daily-23-r2.ps1" -Date $effectiveDate 2>&1 | ForEach-Object { Log ("step1> " + $_.ToString()) }

  Log "step2: 抖音增强（本机下载MP4→Gemini→写R2）"
  if (Is-Port-Open -Host "127.0.0.1" -Port 8080) {
    & ".\\run-daily-newsource-douyin-local.ps1" -Date $effectiveDate -Concurrency $DouyinConcurrency 2>&1 | ForEach-Object { Log ("step2> " + $_.ToString()) }
  } else {
    Log "step2: 跳过（video_spider 未监听 127.0.0.1:8080）"
  }

  Log "step3: 网页正文增强（抓取正文→写R2）"
  & ".\\run-daily-newsource-web.ps1" -Date $effectiveDate -Concurrency $WebConcurrency 2>&1 | ForEach-Object { Log ("step3> " + $_.ToString()) }

  Log "Done."
} catch {
  Log ("FAILED: {0}" -f $_.Exception.Message)
  throw
}

