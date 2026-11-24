param(
    [int] $Port = 8765,
    [string] $DataDirectory,
    [string] $SampleConfig,
    [string] $SyncConfig,
    [string] $LibraryConfig,
    [string] $LanAddress,
    [switch] $LocalOnly
)

$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$work = Join-Path $workspace 'work'
if (-not $DataDirectory) { $DataDirectory = Join-Path $work 'sqa-pilot-data' }
if (-not $SampleConfig) { $SampleConfig = Join-Path $work 'pilot-sources.json' }
if (-not $SyncConfig) { $SyncConfig = Join-Path $work 'pilot-sync.json' }
if (-not $LibraryConfig) { $LibraryConfig = Join-Path $work 'pilot-library.json' }

$python = Get-Command python.exe -ErrorAction Stop
$entry = Join-Path $PSScriptRoot 'app.py'
$arguments = @('-B', $entry, '--data-dir', $DataDirectory, 'serve', '--port', $Port)
$networkConfig = Join-Path $work 'pilot-network.json'
if (-not $LocalOnly) {
    if (-not $LanAddress -and (Test-Path -LiteralPath $networkConfig -PathType Leaf)) {
        $LanAddress = (Get-Content -LiteralPath $networkConfig -Raw | ConvertFrom-Json).lan_address
    }
    if ($LanAddress) { $arguments += @('--lan-ip', $LanAddress) }
}
if (Test-Path -LiteralPath $SampleConfig -PathType Leaf) {
    $arguments += @('--sample-config', $SampleConfig)
}
if (Test-Path -LiteralPath $SyncConfig -PathType Leaf) {
    $arguments += @('--sync-config', $SyncConfig)
}
if (Test-Path -LiteralPath $LibraryConfig -PathType Leaf) {
    $arguments += @('--library-config', $LibraryConfig)
}

Write-Host ('Open http://127.0.0.1:{0}/ in this computer.' -f $Port)
& $python.Source @arguments
exit $LASTEXITCODE
