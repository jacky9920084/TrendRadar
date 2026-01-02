param(
  [string]$ProjectDir = (Split-Path -Parent $MyInvocation.MyCommand.Path),
  [string]$PythonExe = "python",
  [string]$R2InfoFile = "",
  [string]$Date = "",
  [switch]$ValidateOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
# 本脚本用于“抓取+导出+上传”，不需要 TrendRadar 通知推送（避免无渠道时的输出噪音/编码问题）
$env:ENABLE_NOTIFICATION = "0"

function Read-Utf8Text {
  param([Parameter(Mandatory = $true)][string]$Path)
  return [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8)
}

function Get-ShanghaiIsoDate {
  $tz = [TimeZoneInfo]::FindSystemTimeZoneById("China Standard Time")
  $now = [TimeZoneInfo]::ConvertTime([DateTime]::UtcNow, $tz)
  return $now.ToString("yyyy-MM-dd")
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

Set-Location $ProjectDir

$venvPython = Join-Path $ProjectDir ".venv\\Scripts\\python.exe"
if ($PythonExe -eq "python" -and (Test-Path -LiteralPath $venvPython)) {
  $PythonExe = $venvPython
}

$effectiveDate = if ($Date) { $Date } elseif ($env:DATE) { $env:DATE } else { Get-ShanghaiIsoDate }

if (-not $R2InfoFile) {
  $candidates = Get-ChildItem -LiteralPath $ProjectDir -File -Filter "*.md" | Where-Object { $_.Name -match "(?i)r2|s3" }
  if (-not $candidates) {
    $candidates = Get-ChildItem -LiteralPath $ProjectDir -File -Filter "*.md"
  }

  $picked = $null
  foreach ($f in $candidates) {
    try {
      $txt = Read-Utf8Text -Path $f.FullName
      if ($txt -match '(?i)"account_id"\s*:' -and $txt -match '(?i)"access_key_id"\s*:' -and $txt -match '(?i)"secret_access_key"\s*:') {
        $picked = $f.FullName
        break
      }
    } catch {
      continue
    }
  }

  if (-not $picked) {
    throw "R2 info file not found. Pass -R2InfoFile <path>."
  }
  $R2InfoFile = $picked
}

if (-not (Test-Path -LiteralPath $R2InfoFile)) {
  throw "R2 info file missing: $R2InfoFile"
}

$text = Read-Utf8Text -Path $R2InfoFile

$accountId = Get-JsonValueFromText -Text $text -Key "account_id"
$bucket = Get-JsonValueFromText -Text $text -Key "bucket_name"
$ak = Get-JsonValueFromText -Text $text -Key "access_key_id"
$sk = Get-JsonValueFromText -Text $text -Key "secret_access_key"

if (-not $accountId) { throw "Missing account_id in R2 info file." }
if (-not $bucket) { throw "Missing bucket_name in R2 info file." }
if (-not $ak) { throw "Missing access_key_id in R2 info file." }
if (-not $sk) { throw "Missing secret_access_key in R2 info file." }

$endpoint = "https://$accountId.r2.cloudflarestorage.com"
$region = "auto"

if ($ValidateOnly) {
  Write-Output "OK: R2 config parsed (account_id/bucket/access_key/secret_key present)"
  exit 0
}

$env:S3_ENDPOINT_URL = $endpoint
$env:S3_BUCKET_NAME = $bucket
$env:S3_ACCESS_KEY_ID = $ak
$env:S3_SECRET_ACCESS_KEY = $sk
$env:S3_REGION = $region

# TopHub key（用于网易等新源 merge 进主清单；不要写死到仓库）
if (-not $env:TOPHUB_API_KEY) {
  $localKeyPath = Join-Path $ProjectDir "config\\tophub_api_key.local.txt"
  if (Test-Path -LiteralPath $localKeyPath) {
    try { $env:TOPHUB_API_KEY = (Read-Utf8Text -Path $localKeyPath).Trim() } catch {}
  }
}

& $PythonExe -m trendradar

# 抖音主源切换：用 TopHub 抖音榜单替换旧源抖音短链（其他平台不变），确保后续可按 videoId 命中增强材料。
& $PythonExe .\\merge_douyin_into_hotspots.py --date $effectiveDate

# 网易新闻补充：将 TopHub「网易新闻实时热榜」合并进主清单（保持主清单总条数不变），让 Step3/4 真正用到。
if ($env:TOPHUB_API_KEY) {
  & $PythonExe .\\merge_netease_into_hotspots.py --date $effectiveDate --hashid ENeYa4DeY4 --platform 网易新闻 --limit 20 --keep-total 0
} else {
  Write-Output "skip merge_netease_into_hotspots: missing TOPHUB_API_KEY (set env var or create config\\tophub_api_key.local.txt)"
}
