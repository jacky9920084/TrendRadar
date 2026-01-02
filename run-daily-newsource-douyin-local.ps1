param(
  [string]$ProjectDir = (Split-Path -Parent $MyInvocation.MyCommand.Path),
  [string]$PythonExe = "python",
  [string]$R2InfoFile = "",
  [string]$HotspotBase = "",
  [string]$VideoSpiderBase = "",
  [string]$GeminiApiKey = "",
  [string]$GeminiModel = "",
  [string]$GeminiPromptPath = "",
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

function Get-UserEnvOrEmpty {
  param([Parameter(Mandatory = $true)][string]$Name)
  $v = [Environment]::GetEnvironmentVariable($Name, "User")
  if ($v) { return [string]$v }
  return ""
}

function Get-MachineEnvOrEmpty {
  param([Parameter(Mandatory = $true)][string]$Name)
  $v = [Environment]::GetEnvironmentVariable($Name, "Machine")
  if ($v) { return [string]$v }
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

function Find-GeminiPromptPath {
  param([Parameter(Mandatory = $true)][string]$HotspotSparkDir)
  try {
    $candidates = Get-ChildItem -LiteralPath $HotspotSparkDir -File -Filter "gemini*.md" -ErrorAction Stop
  } catch {
    return ""
  }

  foreach ($f in $candidates) {
    try {
      $content = Get-Content -Raw -Encoding utf8 -LiteralPath $f.FullName
    } catch {
      continue
    }
    # The extraction prompt must mention "why_hot" (ASCII) and should not be the final-output template.
    if ($content -match "why_hot") {
      return $f.FullName
    }
  }

  return ""
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
$logPath = Join-Path $outDir ("newsource-douyin-local-{0}.log" -f $effectiveDate)

Write-Output ("TrendRadar newsource-douyin-local: date={0}; concurrency={1}; log={2}" -f $effectiveDate, $effectiveConcurrency, $logPath)
Write-Log -LogPath $logPath -Message ("Start newsource-douyin-local; date={0}; concurrency={1}" -f $effectiveDate, $effectiveConcurrency)

Set-R2EnvFromInfoFile -ProjectDir $ProjectDir -R2InfoFile $R2InfoFile
Write-Log -LogPath $logPath -Message ("R2 config set (bucket={0})" -f $env:S3_BUCKET_NAME)

$effectiveHotspotBase = if ($HotspotBase) { $HotspotBase } elseif ($env:HOTSPARK_BASE) { $env:HOTSPARK_BASE } else { "https://hot-sparks.jacky.onl" }
$effectiveSpiderBase = if ($VideoSpiderBase) { $VideoSpiderBase } elseif ($env:VIDEO_SPIDER_BASE) { $env:VIDEO_SPIDER_BASE } else { "http://127.0.0.1:8080" }

$userGem = Get-UserEnvOrEmpty -Name "GEMINI_API_KEY"
$machineGem = Get-MachineEnvOrEmpty -Name "GEMINI_API_KEY"
$userGoogle = Get-UserEnvOrEmpty -Name "GOOGLE_API_KEY"
$machineGoogle = Get-MachineEnvOrEmpty -Name "GOOGLE_API_KEY"

$effectiveGeminiKey = if ($GeminiApiKey) { $GeminiApiKey } elseif ($env:GEMINI_API_KEY) { $env:GEMINI_API_KEY } elseif ($userGem) { $userGem } elseif ($machineGem) { $machineGem } elseif ($env:GOOGLE_API_KEY) { $env:GOOGLE_API_KEY } elseif ($userGoogle) { $userGoogle } elseif ($machineGoogle) { $machineGoogle } else { "" }
$effectiveGeminiModel = if ($GeminiModel) { $GeminiModel } elseif ($env:GEMINI_MODEL) { $env:GEMINI_MODEL } else { "gemini-3-flash-preview" }
$defaultPrompt = "E:\\cursor\\Hotspot-Spark\\gemini提示词（提取画面+文案）.md"
$effectivePrompt = if ($GeminiPromptPath) { $GeminiPromptPath } elseif ($env:GEMINI_PROMPT_PATH) { $env:GEMINI_PROMPT_PATH } else { $defaultPrompt }

$effectiveHotspotBase = $effectiveHotspotBase.TrimEnd("/")
$effectiveSpiderBase = $effectiveSpiderBase.TrimEnd("/")
$effectiveGeminiKey = $effectiveGeminiKey.Trim()

if (-not $effectiveGeminiKey) { throw "Missing GEMINI_API_KEY (or GOOGLE_API_KEY). Required for local extraction." }
if (-not (Test-Path -LiteralPath $effectivePrompt)) {
  $autoPrompt = Find-GeminiPromptPath -HotspotSparkDir "E:\\cursor\\Hotspot-Spark"
  if ($autoPrompt) {
    $effectivePrompt = $autoPrompt
  } else {
    throw "Missing Gemini prompt file: $effectivePrompt"
  }
}

$env:HOTSPARK_BASE = $effectiveHotspotBase
$env:VIDEO_SPIDER_BASE = $effectiveSpiderBase
$env:GEMINI_API_KEY = $effectiveGeminiKey
$env:GEMINI_MODEL = $effectiveGeminiModel
$env:GEMINI_PROMPT_PATH = $effectivePrompt

if ($Force) { $env:FORCE = "1" }

try {
  $oldErrPref = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  $pyOut = & $PythonExe .\\newsource_douyin_local.py --date $effectiveDate --concurrency $effectiveConcurrency --log $logPath 2>&1
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
