$root = $PSScriptRoot
$pidPath = Join-Path $root 'data\server.pid'
$appPath = Join-Path $root 'app.py'
if (-not (Test-Path -LiteralPath $pidPath)) {
    Write-Output 'Image Atelier is not running, or was started from an older launcher.'
    exit 0
}
try { $serverId = [int](Get-Content -LiteralPath $pidPath -Raw).Trim() } catch { $serverId = 0 }
$process = if ($serverId) { Get-CimInstance Win32_Process -Filter "ProcessId = $serverId" -ErrorAction SilentlyContinue } else { $null }
if ($process -and $process.CommandLine -and $process.CommandLine.Contains($appPath)) {
    Stop-Process -Id $serverId -Force -ErrorAction SilentlyContinue
    Write-Output 'Image Atelier stopped. Your saved settings and images remain in data\.'
} else {
    Write-Output 'The recorded server process is already stopped.'
}
Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
