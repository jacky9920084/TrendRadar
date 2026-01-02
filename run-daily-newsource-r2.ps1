param(
  [string]$ProjectDir = (Split-Path -Parent $MyInvocation.MyCommand.Path),
  [string]$R2InfoFile = "",
  [string]$HotspotBase = "",
  [string]$VideoSpiderBase = "",
  [string]$IngestToken = "",
  [int]$Concurrency = 0,
  [string]$Date = ""
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

function Invoke-RestJson {
  param(
    [Parameter(Mandatory = $true)][string]$Method,
    [Parameter(Mandatory = $true)][string]$Url,
    [hashtable]$Headers = @{},
    [string]$Body = "",
    [string]$ContentType = "",
    [int]$TimeoutSec = 30
  )

  $params = @{
    Method     = $Method
    Uri        = $Url
    Headers    = $Headers
    TimeoutSec = $TimeoutSec
  }
  if ($Body) { $params["Body"] = $Body }
  if ($ContentType) { $params["ContentType"] = $ContentType }

  return Invoke-RestMethod @params
}

function Invoke-WithRetry {
  param(
    [Parameter(Mandatory = $true)][scriptblock]$Action,
    [int]$Retries = 2,
    [int]$BaseSleepMs = 800
  )

  $last = $null
  for ($i = 0; $i -le $Retries; $i++) {
    try {
      return & $Action
    } catch {
      $last = $_
      if ($i -ge $Retries) { break }
      $sleep = [Math]::Min(10000, $BaseSleepMs * [Math]::Pow(2, $i))
      Start-Sleep -Milliseconds ([int]$sleep)
    }
  }
  throw $last
}

function Test-TcpReachable {
  param([Parameter(Mandatory = $true)][string]$BaseUrl)
  $u = [Uri]$BaseUrl
  $hostName = $u.Host
  $port = if ($u.IsDefaultPort) { if ($u.Scheme -eq "https") { 443 } else { 80 } } else { $u.Port }
  $client = New-Object System.Net.Sockets.TcpClient
  try {
    $iar = $client.BeginConnect($hostName, $port, $null, $null)
    if (-not $iar.AsyncWaitHandle.WaitOne(1200, $false)) {
      try { $client.Close() } catch {}
      return $false
    }
    $client.EndConnect($iar) | Out-Null
    return $true
  } catch {
    return $false
  } finally {
    try { $client.Close() } catch {}
  }
}

Set-Location $ProjectDir

$effectiveHotspotBase = if ($HotspotBase) { $HotspotBase } elseif ($env:HOTSPARK_BASE) { $env:HOTSPARK_BASE } else { "https://hot-sparks.jacky.onl" }
$effectiveSpiderBase = if ($VideoSpiderBase) { $VideoSpiderBase } elseif ($env:VIDEO_SPIDER_BASE) { $env:VIDEO_SPIDER_BASE } else { "http://127.0.0.1:8080" }
$effectiveToken = if ($IngestToken) { $IngestToken } elseif ($env:INGEST_TOKEN) { $env:INGEST_TOKEN } else { "" }
$effectiveConcurrency = if ($Concurrency -gt 0) { $Concurrency } elseif ($env:CONCURRENCY) { [int]$env:CONCURRENCY } else { 2 }
$effectiveDate = if ($Date) { $Date } elseif ($env:DATE) { $env:DATE } else { Get-ShanghaiIsoDate }

$effectiveHotspotBase = $effectiveHotspotBase.TrimEnd("/")
$effectiveSpiderBase = $effectiveSpiderBase.TrimEnd("/")
$effectiveToken = $effectiveToken.Trim()

Assert-IsoDate -DateStr $effectiveDate
if (-not $effectiveToken) { throw "Missing INGEST_TOKEN. Set env var INGEST_TOKEN or pass -IngestToken." }
if ($effectiveConcurrency -lt 1) { $effectiveConcurrency = 1 }
if ($effectiveConcurrency -gt 3) { $effectiveConcurrency = 3 }

$outDir = Join-Path $ProjectDir "output"
if (-not (Test-Path -LiteralPath $outDir)) { New-Item -ItemType Directory -Path $outDir | Out-Null }
$logPath = Join-Path $outDir ("newsource-{0}.log" -f $effectiveDate)

Write-Output ("TrendRadar newsource: date={0}; log={1}" -f $effectiveDate, $logPath)
Write-Log -LogPath $logPath -Message ("Start newsource; date={0}" -f $effectiveDate)

# 1) Parse R2 config (same style as run-daily-23-r2.ps1)
try {
  if (-not $R2InfoFile) {
    $candidates = Get-ChildItem -LiteralPath $ProjectDir -File -Filter "*.md" | Where-Object { $_.Name -match "(?i)r2|s3" }
    if (-not $candidates) { $candidates = Get-ChildItem -LiteralPath $ProjectDir -File -Filter "*.md" }

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
    if (-not $picked) { throw "R2 info file not found. Pass -R2InfoFile <path>." }
    $R2InfoFile = $picked
  }

  if (-not (Test-Path -LiteralPath $R2InfoFile)) { throw "R2 info file missing: $R2InfoFile" }

  $text = Read-Utf8Text -Path $R2InfoFile
  $accountId = Get-JsonValueFromText -Text $text -Key "account_id"
  $bucket = Get-JsonValueFromText -Text $text -Key "bucket_name"
  $ak = Get-JsonValueFromText -Text $text -Key "access_key_id"
  $sk = Get-JsonValueFromText -Text $text -Key "secret_access_key"

  if (-not $accountId -or -not $bucket -or -not $ak -or -not $sk) { throw "R2 info file missing required fields." }

  $env:S3_ENDPOINT_URL = "https://$accountId.r2.cloudflarestorage.com"
  $env:S3_BUCKET_NAME = $bucket
  $env:S3_ACCESS_KEY_ID = $ak
  $env:S3_SECRET_ACCESS_KEY = $sk
  $env:S3_REGION = "auto"
  Write-Log -LogPath $logPath -Message ("R2 config parsed OK (file={0})" -f $R2InfoFile)
} catch {
  # 本任务本身不依赖 R2 直连；解析失败不应阻塞抖音增强链路。
  Write-Log -LogPath $logPath -Message ("R2 config parse SKIPPED: {0}" -f ($_.Exception.Message))
}

# 2) Preflight local spider
if (-not (Test-TcpReachable -BaseUrl $effectiveSpiderBase)) {
  $msg = "video_spider not reachable: $effectiveSpiderBase (start the service; expected POST /analysis)."
  Write-Log -LogPath $logPath -Message $msg
  throw $msg
}

# 3) Fetch hotlist
$hotlistUrl = "{0}/api/hotlist/douyin?date={1}&refresh=1" -f $effectiveHotspotBase, $effectiveDate
Write-Log -LogPath $logPath -Message ("Fetch hotlist: {0}" -f $hotlistUrl)

$hotlist = Invoke-WithRetry -Retries 2 -Action {
  Invoke-RestJson -Method "GET" -Url $hotlistUrl -TimeoutSec 30
}

if (-not $hotlist -or -not $hotlist.ok) {
  $msg = "Hotlist API failed: $hotlistUrl"
  Write-Log -LogPath $logPath -Message $msg
  throw $msg
}

$items = @()
if ($hotlist.items -and ($hotlist.items -is [System.Collections.IEnumerable])) { $items = @($hotlist.items) }
Write-Log -LogPath $logPath -Message ("Hotlist items: {0}" -f $items.Count)
if ($items.Count -eq 0) {
  Write-Log -LogPath $logPath -Message "No items; exit 0."
  Write-Output "No hotlist items."
  exit 0
}

# 4) Resolve mp4_url via local spider
$resolved = New-Object System.Collections.Generic.List[object]
$failed = New-Object System.Collections.Generic.List[object]

function Resolve-One {
  param([Parameter(Mandatory = $true)]$Item)
  $itemId = [string]$Item.item_id
  $title = [string]$Item.title
  $videoUrl = [string]$Item.url
  if (-not $videoUrl) { $videoUrl = [string]$Item.video_url }
  $videoUrl = ([string]$videoUrl).Trim()
  if (-not $itemId -or -not $videoUrl) { throw "Missing item_id or url." }

  $endpoint = "{0}/analysis" -f $effectiveSpiderBase
  $body = "share_link={0}" -f [Uri]::EscapeDataString($videoUrl)

  $resp = Invoke-WithRetry -Retries 2 -Action {
    Invoke-RestJson -Method "POST" -Url $endpoint -ContentType "application/x-www-form-urlencoded" -Body $body -TimeoutSec 60
  }

  $rp = $resp.data.resource_path
  if ($rp -is [string]) {
    $mp4 = $rp
  } elseif ($rp -is [System.Collections.IEnumerable]) {
    throw "video_spider returned non-video resource_path (array)."
  } else {
    $mp4 = [string]$rp
  }

  $spiderTitle = [string]$resp.data.title
  if (-not $mp4) { throw "video_spider returned empty data.resource_path." }
  $mp4 = $mp4.Trim()
  if (-not $mp4) { throw "video_spider returned blank data.resource_path." }

  $finalTitle = if ($spiderTitle -and $spiderTitle.Trim()) { $spiderTitle.Trim() } else { ([string]$title).Trim() }
  $cover = [string]$resp.data.cover
  $finalThumb = if ($cover -and $cover.Trim()) { $cover.Trim() } else { [string]$Item.thumbnail }

  return [pscustomobject]@{
    item_id    = $itemId.Trim()
    title      = $finalTitle
    video_url  = $videoUrl
    mp4_url    = $mp4
    extra      = $Item.extra
    time       = $Item.time
    thumbnail  = $finalThumb
  }
}

if ($PSVersionTable.PSVersion.Major -ge 7 -and $effectiveConcurrency -gt 1) {
  Write-Log -LogPath $logPath -Message ("Resolve via video_spider in parallel (concurrency={0})" -f $effectiveConcurrency)
  $results = $items | ForEach-Object -Parallel {
    try {
      $r = & $using:Resolve-One -Item $_
      [pscustomobject]@{ ok = $true; item = $r; err = "" }
    } catch {
      [pscustomobject]@{ ok = $false; item = $_; err = $_.Exception.Message }
    }
  } -ThrottleLimit $effectiveConcurrency

  foreach ($r in $results) {
    if ($r.ok) { $resolved.Add($r.item) | Out-Null } else { $failed.Add([pscustomobject]@{ item = $r.item; error = $r.err }) | Out-Null }
  }
} else {
  Write-Log -LogPath $logPath -Message ("Resolve via video_spider sequential (concurrency={0})" -f $effectiveConcurrency)
  foreach ($it in $items) {
    try {
      $r = Resolve-One -Item $it
      $resolved.Add($r) | Out-Null
    } catch {
      $failed.Add([pscustomobject]@{ item = $it; error = $_.Exception.Message }) | Out-Null
      continue
    }
  }
}

Write-Log -LogPath $logPath -Message ("Resolved: {0}; Failed: {1}" -f $resolved.Count, $failed.Count)

if ($failed.Count -gt 0) {
  $sample = $failed | Select-Object -First 5
  foreach ($f in $sample) {
    $id = [string]$f.item.item_id
    $u = [string]$f.item.url
    Write-Log -LogPath $logPath -Message ("Resolve failed: item_id={0}; url={1}; err={2}" -f $id, $u, $f.error)
  }
}

if ($resolved.Count -eq 0) {
  Write-Log -LogPath $logPath -Message "No resolved items; exit 0."
  Write-Output "No resolved items; nothing to ingest."
  exit 0
}

# 5) Batch ingest
$ingestUrl = "{0}/api/douyin/ingest" -f $effectiveHotspotBase
$headers = @{ "X-INGEST-TOKEN" = $effectiveToken }

$batchSize = 20
$ingestCreated = 0
$ingestUpdated = 0
$ingestSkipped = 0

for ($i = 0; $i -lt $resolved.Count; $i += $batchSize) {
  $batch = $resolved.GetRange($i, [Math]::Min($batchSize, $resolved.Count - $i))
  $payload = @{ date = $effectiveDate; items = $batch } | ConvertTo-Json -Depth 12
  Write-Log -LogPath $logPath -Message ("POST ingest batch: {0} items" -f $batch.Count)

  $resp = Invoke-WithRetry -Retries 2 -Action {
    Invoke-RestJson -Method "POST" -Url $ingestUrl -Headers $headers -ContentType "application/json" -Body $payload -TimeoutSec 60
  }

  if (-not $resp -or -not $resp.ok) {
    Write-Log -LogPath $logPath -Message ("Ingest batch failed: {0}" -f $ingestUrl)
    continue
  }

  foreach ($it in @($resp.items)) {
    $st = [string]$it.status
    if ($st -eq "created") { $ingestCreated++ }
    elseif ($st -eq "updated") { $ingestUpdated++ }
    elseif ($st -eq "skipped") { $ingestSkipped++ }
  }
}

Write-Log -LogPath $logPath -Message ("Ingest summary: created={0}; updated={1}; skipped={2}" -f $ingestCreated, $ingestUpdated, $ingestSkipped)

# 6) Probe materials progress (best effort)
try {
  $materialsUrl = "{0}/api/douyin/materials?date={1}" -f $effectiveHotspotBase, $effectiveDate
  $m = Invoke-WithRetry -Retries 1 -Action { Invoke-RestJson -Method "GET" -Url $materialsUrl -TimeoutSec 30 }
  if ($m -and $m.ok) {
    $ready = $m.stats.materials_ready
    $total = $m.stats.materials_total
    Write-Log -LogPath $logPath -Message ("Materials progress: ready={0}; total={1}" -f $ready, $total)
  }
} catch {
  Write-Log -LogPath $logPath -Message ("Materials probe failed: {0}" -f $_.Exception.Message)
}

Write-Log -LogPath $logPath -Message "Done."
Write-Output ("Done. Resolved={0}; Failed={1}; Ingest created={2} updated={3} skipped={4}. Log={5}" -f $resolved.Count, $failed.Count, $ingestCreated, $ingestUpdated, $ingestSkipped, $logPath)
