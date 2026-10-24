$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$data = Join-Path $root 'data'
New-Item -ItemType Directory -Force -Path $data | Out-Null
$base = 'http://127.0.0.1:7860'

function Test-ImageAtelier {
    try {
        $settings = Invoke-RestMethod -Uri "$base/api/settings" -TimeoutSec 2
        return [bool]$settings.profile_id
    } catch {
        return $false
    }
}

if (Test-ImageAtelier) {
    Start-Process $base
    exit 0
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
    if (Test-ImageAtelier) {
        Start-Process $base
        exit 0
    }
    if ($process.HasExited) { break }
    Start-Sleep -Milliseconds 500
}
Write-Error "Image Atelier did not start. Check $stderr"
exit 1
