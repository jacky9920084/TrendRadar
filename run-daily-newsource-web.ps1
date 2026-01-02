param(
  [string]$ProjectDir = (Split-Path -Parent $MyInvocation.MyCommand.Path),
  [string]$PythonExe = "python",
  [string]$R2InfoFile = "",
  [int]$Concurrency = 0,
  [string]$Date = "",
  [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

function Read-Utf8Text {
  param([Parameter(Mandatory = $true)][string]$Path)
  return [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8)
}

function Get-JsonValueFromText {
  param(
    [Parameter(Mandatory = $true)][string]$Text,
    [Parameter(Mandatory = $true)][string]$Key
  )
  $m = [regex]::Match($Text, '(?im)^\s*"' + [regex]::Escape($Key) + '"\s*:\s*"([^"]+)"\s*,?\s*$')
  if ($m.Success) { return $m.Groups[1].Value.Trim() }
  return ""
}

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

Set-Location $ProjectDir

function Resolve-R2InfoFile {
  param([Parameter(Mandatory = $true)][string]$ProjectDir, [string]$R2InfoFile)

  if ($R2InfoFile -and (Test-Path -LiteralPath $R2InfoFile)) { return $R2InfoFile }

  $preferred = Join-Path $ProjectDir "config\\r2_info.local.md"
  if (Test-Path -LiteralPath $preferred) { return $preferred }

  $candidates = Get-ChildItem -LiteralPath $ProjectDir -File -Filter "*.md" | Where-Object { $_.Name -match "(?i)r2|s3" }
  if (-not $candidates) { $candidates = Get-ChildItem -LiteralPath $ProjectDir -File -Filter "*.md" }

  foreach ($f in $candidates) {
    try { $txt = Read-Utf8Text -Path $f.FullName } catch { continue }
    if ($txt -match '(?i)"account_id"\s*:' -and $txt -match '(?i)"access_key_id"\s*:' -and $txt -match '(?i)"secret_access_key"\s*:') {
      return $f.FullName
    }
  }

  return ""
}

function Set-R2EnvFromInfoFile {
  param([Parameter(Mandatory = $true)][string]$ProjectDir, [string]$R2InfoFile)

  if (($env:S3_ENDPOINT_URL) -and ($env:S3_BUCKET_NAME) -and ($env:S3_ACCESS_KEY_ID) -and ($env:S3_SECRET_ACCESS_KEY)) {
    return
  }

  $picked = Resolve-R2InfoFile -ProjectDir $ProjectDir -R2InfoFile $R2InfoFile
  if (-not $picked) { throw "R2 info file not found. Create config\\r2_info.local.md (not committed) or pass -R2InfoFile <path>." }

  $text = Read-Utf8Text -Path $picked
  $accountId = Get-JsonValueFromText -Text $text -Key "account_id"
  $bucket = Get-JsonValueFromText -Text $text -Key "bucket_name"
  $ak = Get-JsonValueFromText -Text $text -Key "access_key_id"
  $sk = Get-JsonValueFromText -Text $text -Key "secret_access_key"

  if (-not $accountId) { throw "Missing account_id in R2 info file: $picked" }
  if (-not $bucket) { throw "Missing bucket_name in R2 info file: $picked" }
  if (-not $ak) { throw "Missing access_key_id in R2 info file: $picked" }
  if (-not $sk) { throw "Missing secret_access_key in R2 info file: $picked" }

  $env:S3_ENDPOINT_URL = "https://$accountId.r2.cloudflarestorage.com"
  $env:S3_BUCKET_NAME = $bucket
  $env:S3_ACCESS_KEY_ID = $ak
  $env:S3_SECRET_ACCESS_KEY = $sk
  $env:S3_REGION = "auto"
}

function Set-TopHubEnv {
  param([Parameter(Mandatory = $true)][string]$ProjectDir)
  if ($env:TOPHUB_API_KEY) { return }
  $localKeyPath = Join-Path $ProjectDir "config\\tophub_api_key.local.txt"
  if (Test-Path -LiteralPath $localKeyPath) {
    try { $env:TOPHUB_API_KEY = (Read-Utf8Text -Path $localKeyPath).Trim() } catch {}
  }
}

$venvPython = Join-Path $ProjectDir ".venv\\Scripts\\python.exe"
if ($PythonExe -eq "python" -and (Test-Path -LiteralPath $venvPython)) {
  $PythonExe = $venvPython
}

$effectiveDate = if ($Date) { $Date } elseif ($env:DATE) { $env:DATE } else { Get-ShanghaiIsoDate }
Assert-IsoDate -DateStr $effectiveDate

$effectiveConcurrency = if ($Concurrency -gt 0) { $Concurrency } elseif ($env:CONCURRENCY) { [int]$env:CONCURRENCY } else { 2 }
if ($effectiveConcurrency -lt 1) { $effectiveConcurrency = 1 }
if ($effectiveConcurrency -gt 3) { $effectiveConcurrency = 3 }

$outDir = Join-Path $ProjectDir "output"
if (-not (Test-Path -LiteralPath $outDir)) { New-Item -ItemType Directory -Path $outDir | Out-Null }
$logPath = Join-Path $outDir ("newsource-web-{0}.log" -f $effectiveDate)

Write-Output ("TrendRadar newsource-web: date={0}; concurrency={1}; log={2}" -f $effectiveDate, $effectiveConcurrency, $logPath)
Write-Log -LogPath $logPath -Message ("Start newsource-web; date={0}; concurrency={1}" -f $effectiveDate, $effectiveConcurrency)

Set-R2EnvFromInfoFile -ProjectDir $ProjectDir -R2InfoFile $R2InfoFile
Set-TopHubEnv -ProjectDir $ProjectDir

Write-Log -LogPath $logPath -Message ("R2 config set (bucket={0})" -f $env:S3_BUCKET_NAME)

if ($Force) { $env:FORCE = "1" }

try {
  $oldErrPref = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  $pyOut = & $PythonExe .\\newsource_web.py --date $effectiveDate --concurrency $effectiveConcurrency --log $logPath 2>&1
  $ErrorActionPreference = $oldErrPref
  $exitCode = $LASTEXITCODE
  $pyText = ($pyOut | Out-String).Trim()
  if ($pyText) {
    Write-Log -LogPath $logPath -Message ("Python summary: {0}" -f $pyText)
    Write-Output $pyText
  }
  if ($exitCode -ne 0) { throw "Python task failed with exit code $exitCode" }
} catch {
  Write-Log -LogPath $logPath -Message ("FAILED: {0}" -f $_.Exception.Message)
  throw
} finally {
  Write-Log -LogPath $logPath -Message "Done."
}
