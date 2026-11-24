param(
    [ValidateRange(1024, 65535)] [int] $Port = 8765,
    [string] $DataDirectory,
    [string] $LanAddress,
    [switch] $LocalOnly
)

$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$work = Join-Path $workspace 'work'
if (-not $DataDirectory) { $DataDirectory = Join-Path $work 'sqa-pilot-data' }
$DataDirectory = [IO.Path]::GetFullPath($DataDirectory)
$entry = Join-Path $PSScriptRoot 'app.py'
$python = Get-Command python.exe -ErrorAction Stop
$networkConfig = Join-Path $work 'pilot-network.json'
if (-not $LocalOnly -and -not $LanAddress -and (Test-Path -LiteralPath $networkConfig)) {
    $LanAddress = (Get-Content -LiteralPath $networkConfig -Raw | ConvertFrom-Json).lan_address
}
if (-not $LocalOnly) {
    if (-not $LanAddress) { throw 'The office LAN address is missing in work/pilot-network.json.' }
    $address = [Net.IPAddress]::Parse($LanAddress)
    $bytes = $address.GetAddressBytes()
    $private = $bytes.Length -eq 4 -and ($bytes[0] -eq 10 -or
        ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31) -or
        ($bytes[0] -eq 192 -and $bytes[1] -eq 168))
    if (-not $private) { throw 'An explicit private office IPv4 address is required.' }
    $localAddresses = [Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces() |
        ForEach-Object { $_.GetIPProperties().UnicastAddresses } |
        ForEach-Object { $_.Address.ToString() }
    if ($LanAddress -notin $localAddresses) { throw 'The configured office IP is not assigned to this computer.' }
}
if (-not (Test-Path -LiteralPath (Join-Path $DataDirectory 'pilot.sqlite3'))) {
    throw 'The pilot database was not found. Check DataDirectory before restarting.'
}

# Match the exact application and data directory before stopping an existing listener.
$listenerIds = @()
$allowedAddresses = @('127.0.0.1')
if (-not $LocalOnly) { $allowedAddresses += $LanAddress }
foreach ($line in (netstat -ano -p tcp)) {
    if ($line -match '^\s*TCP\s+(\S+):(\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$' -and
        [int]$Matches[2] -eq $Port) {
        if ($Matches[1] -notin $allowedAddresses) {
            throw "Unexpected listener on port $Port. Inspect it before restarting."
        }
        $listenerIds += [int]$Matches[3]
    }
}
$listenerIds = @($listenerIds | Select-Object -Unique)
$receiptPath = Join-Path $work "pilot-service-$Port.json"
$saved = $null
if (Test-Path -LiteralPath $receiptPath) {
    try { $saved = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json } catch { $saved = $null }
}
foreach ($listenerId in $listenerIds) {
    $registered = $false
    if ($saved -and $saved.pid -eq $listenerId -and $saved.data_directory -eq $DataDirectory -and
        $saved.entry -eq $entry -and $saved.process_start_ticks) {
        try {
            $existing = Get-Process -Id $listenerId -ErrorAction Stop
            $registered = $existing.Path -eq $saved.executable -and
                $existing.StartTime.ToUniversalTime().Ticks.ToString() -eq $saved.process_start_ticks
        } catch { $registered = $false }
    }
    if ($registered) { continue }
    try {
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $listenerId" -ErrorAction Stop
    } catch {
        throw "Windows denied inspection of service PID $listenerId. Run this script under the Windows account that started the service."
    }
    $command = $processInfo.CommandLine
    $entryPattern = '(?i)(?:^|\s)"?' + [regex]::Escape($entry) + '"?(?:\s|$)'
    $dataPattern = '(?i)(?:^|\s)"?' + [regex]::Escape($DataDirectory) + '"?(?:\s|$)'
    if (-not $command -or $command -notmatch $entryPattern -or $command -notmatch $dataPattern) {
        throw "PID $listenerId could not be verified as this pilot service."
    }
}

& $python.Source -B $entry --data-dir $DataDirectory backup --reason pre-upgrade
if ($LASTEXITCODE -ne 0) { throw 'The pre-upgrade backup failed. Restart aborted.' }
foreach ($listenerId in $listenerIds) {
    try {
        Stop-Process -Id $listenerId -ErrorAction Stop
        Wait-Process -Id $listenerId -Timeout 15 -ErrorAction SilentlyContinue
    } catch {
        throw "Windows denied stopping service PID $listenerId. Run this script under the Windows account that started the service."
    }
}

$arguments = @('-B', $entry, '--data-dir', $DataDirectory, 'serve', '--port', "$Port")
if (-not $LocalOnly) { $arguments += @('--lan-ip', $LanAddress) }
foreach ($item in @(
    @{ Flag = '--sample-config'; File = 'pilot-sources.json' },
    @{ Flag = '--sync-config'; File = 'pilot-sync.json' },
    @{ Flag = '--library-config'; File = 'pilot-library.json' }
)) {
    $configPath = Join-Path $work $item.File
    if (Test-Path -LiteralPath $configPath) { $arguments += @($item.Flag, $configPath) }
}
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$stdout = Join-Path $work "pilot-$Port-$stamp.stdout.log"
$stderr = Join-Path $work "pilot-$Port-$stamp.stderr.log"
$quotedArguments = $arguments | ForEach-Object { '"' + $_ + '"' }
$service = Start-Process -FilePath $python.Source -ArgumentList $quotedArguments `
    -WorkingDirectory $workspace -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr

$origins = @("http://127.0.0.1:$Port")
if (-not $LocalOnly) { $origins += "http://${LanAddress}:$Port" }
$handler = [Net.Http.HttpClientHandler]::new()
$handler.UseProxy = $false
$client = [Net.Http.HttpClient]::new($handler)
$client.Timeout = [TimeSpan]::FromSeconds(2)
$healthy = $false
try {
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        $service.Refresh()
        if ($service.HasExited) { throw "The service exited. Inspect $stderr" }
        $healthy = $true
        foreach ($origin in $origins) {
            try {
                $response = $client.GetAsync("$origin/api/session").GetAwaiter().GetResult()
                $healthy = $healthy -and $response.IsSuccessStatusCode
                $response.Dispose()
            } catch { $healthy = $false }
        }
        if ($healthy) { break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $healthy) { throw "The service did not become healthy. Inspect $stderr" }
} catch {
    if (-not $service.HasExited) { Stop-Process -Id $service.Id -ErrorAction SilentlyContinue }
    throw
} finally {
    $client.Dispose()
    $handler.Dispose()
}
$receipt = [ordered]@{
    pid = $service.Id
    entry = $entry
    executable = $python.Source
    process_start_ticks = $service.StartTime.ToUniversalTime().Ticks.ToString()
    started_at = [DateTimeOffset]::Now.ToString('o')
    origins = $origins
    data_directory = $DataDirectory
    stdout = $stdout
    stderr = $stderr
}
$receipt | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $work "pilot-service-$Port.json") -Encoding utf8
Write-Host 'Background service is ready.'
$origins | ForEach-Object { Write-Host "$_/" }

