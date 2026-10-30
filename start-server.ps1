$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$data = Join-Path $root 'data'
New-Item -ItemType Directory -Force -Path $data | Out-Null
$base = 'http://127.0.0.1:7860'

function Get-ImageAtelierState {
    try {
        $settings = Invoke-RestMethod -Uri "$base/api/settings" -TimeoutSec 2
        $page = (Invoke-WebRequest -Uri $base -TimeoutSec 2).Content
        return @{ Running = [bool]$settings.profile_id; Current = [bool]$settings.profile_id -and $page.Contains('image-name-1') }
    } catch {
        return @{ Running = $false; Current = $false }
    }
}

$service = Get-ImageAtelierState
if ($service.Current) {
    Start-Process $base
    exit 0
}

if ($service.Running) {
    $pidPath = Join-Path $data 'server.pid'
    $recordedId = 0
    try { $recordedId = [int](Get-Content -LiteralPath $pidPath -Raw).Trim() } catch {}
    $process = if ($recordedId) { Get-CimInstance Win32_Process -Filter "ProcessId = $recordedId" -ErrorAction SilentlyContinue } else { $null }
    if (-not $process -or -not $process.CommandLine -or -not $process.CommandLine.Contains((Join-Path $root 'app.py'))) {
        throw 'An older Image Atelier service is running. Stop it using the stop launcher, then start again.'
    }
    Stop-Process -Id $recordedId -Force
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline -and (Get-ImageAtelierState).Running) { Start-Sleep -Milliseconds 250 }
}

$pythonCommand = Get-Command python.exe -ErrorAction Stop
$appPath = Join-Path $root 'app.py'
$stdout = Join-Path $data 'server.log'
$stderr = Join-Path $data 'server-error.log'
$pidPath = Join-Path $data 'server.pid'
$process = Start-Process -FilePath $pythonCommand.Source -ArgumentList ('"{0}"' -f $appPath) -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
Set-Content -LiteralPath $pidPath -Value $process.Id -Encoding ascii

$deadline = (Get-Date).AddSeconds(20)
while ((Get-Date) -lt $deadline) {
    if ((Get-ImageAtelierState).Current) {
        Start-Process $base
        exit 0
    }
    if ($process.HasExited) { break }
    Start-Sleep -Milliseconds 500
}
Write-Error "Image Atelier did not start. Check $stderr"
exit 1
