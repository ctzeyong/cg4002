# Configuration A, laptop half: Mosquitto on this laptop, then the relay. Pane 1 of two;
# pane 2 is run_board_a.ps1. Bring-up and the traps checked below: notes/demo_network.md.
#
#   .\run_pipeline_a.ps1 [--no-tunnel] [-v] [--nodes 1,2] ...
#
# Everything but --no-tunnel is passed through to pipeline.py. The broker starts here and
# stops when the relay exits; its output goes to logs\<UTC date>\mosquitto_<time>Z.log.
# No param block on purpose: it would bind -v to -Verbose instead of forwarding it.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $here
$python = Join-Path $repo ".venv\Scripts\python.exe"
$mosquitto = "C:\Program Files\Mosquitto\mosquitto.exe"
$apAddress = "192.168.137.1"  # the laptop's own hotspot; laptop\mosquitto.conf binds it

foreach ($exe in @($python, $mosquitto)) {
    if (-not (Test-Path $exe)) {
        Write-Error "no executable at $exe"
        exit 1
    }
}

# PowerShell parses `1,2` into an array, which would reach Python as two args.
$passthru = @()
foreach ($a in $args) {
    if ($a -is [array]) { $passthru += ($a -join ",") } else { $passthru += [string]$a }
}
$tunnel = "--tunnel"
$forward = @()
foreach ($a in $passthru) {
    if ($a -eq "--no-tunnel") { $tunnel = $null } else { $forward += $a }
}
# keystroke_reader uses msvcrt, which reads the console rather than stdin.
if ([Console]::IsInputRedirected) {
    Write-Warning "stdin is redirected, so keypresses will send no CMD packet."
    Write-Warning "Run this directly in Windows Terminal for criterion 5."
}

# The service would hold 1883 and answer as the wrong broker, silently.
$service = Get-Service mosquitto -ErrorAction SilentlyContinue
if ($service -and $service.Status -eq "Running") {
    Write-Error "the Mosquitto service is running and holds 1883. In an elevated shell: Stop-Service mosquitto"
    exit 1
}
# Without the hotspot the 192.168.137.1 listener cannot bind, and Mosquitto exits.
if (-not (Get-NetIPAddress -IPAddress $apAddress -ErrorAction SilentlyContinue)) {
    Write-Error "no $apAddress on this laptop: turn on Mobile hotspot first (Settings > Network & Internet)"
    exit 1
}
$holders = @(Get-NetTCPConnection -LocalPort 1883 -State Listen -ErrorAction SilentlyContinue)
if ($holders.Count -gt 0) {
    $ids = $holders.OwningProcess | Sort-Object -Unique
    Write-Error "port 1883 is already held by process $($ids -join ', '). A broker left from an earlier run? Stop-Process -Id <pid>"
    exit 1
}

# A per-run copy of laptop\mosquitto.conf that logs to a file instead of stdout. Redirected
# stdout is block-buffered and lost when Stop-Process ends the broker (checked 2 Oct); the
# file log is written line by line. The copy holds an absolute path, which contains the
# Windows username, so it lives under logs\, which git ignores.
$now = (Get-Date).ToUniversalTime()
$logDir = Join-Path $repo ("logs\" + $now.ToString("yyyy-MM-dd"))
New-Item -ItemType Directory -Force $logDir | Out-Null
$stem = Join-Path $logDir ("mosquitto_" + $now.ToString("HHmmss") + "Z")
$brokerLog = "$stem.log"
$brokerErr = "$stem.err.log"  # only what Mosquitto prints before it reads its config
$brokerConf = "$stem.conf"
$conf = Get-Content (Join-Path $repo "laptop\mosquitto.conf")
if (@($conf | Where-Object { $_ -eq "log_dest stdout" }).Count -ne 1) {
    Write-Error "laptop\mosquitto.conf no longer has exactly one 'log_dest stdout' line to replace"
    exit 1
}
$conf | ForEach-Object { if ($_ -eq "log_dest stdout") { "log_dest file $brokerLog" } else { $_ } } |
    Set-Content -Path $brokerConf -Encoding ascii
# From laptop\, because mosquitto.conf's include_dir is relative to the working directory.
$broker = Start-Process -FilePath $mosquitto -ArgumentList "-c", "`"$brokerConf`"" `
    -WorkingDirectory (Join-Path $repo "laptop") -NoNewWindow -PassThru `
    -RedirectStandardOutput "$stem.stdout.log" -RedirectStandardError $brokerErr

# Both listeners, or nothing: the bind is guideline 9's evidence (demo_network.md step 4).
$deadline = (Get-Date).AddSeconds(5)
do {
    Start-Sleep -Milliseconds 200
    $bound = @(Get-NetTCPConnection -LocalPort 1883 -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.OwningProcess -eq $broker.Id } |
        ForEach-Object { $_.LocalAddress } | Sort-Object -Unique)
    $ready = ($bound -contains "127.0.0.1") -and ($bound -contains $apAddress)
} until ($ready -or $broker.HasExited -or ((Get-Date) -gt $deadline))
if (-not $ready) {
    Write-Error "Mosquitto did not bind 127.0.0.1 and $apAddress (got: $($bound -join ', ')). See $brokerErr"
    Get-Content $brokerErr, $brokerLog -ErrorAction SilentlyContinue | Select-Object -Last 10
    if (-not $broker.HasExited) { Stop-Process -Id $broker.Id -ErrorAction SilentlyContinue }
    exit 1
}
Write-Host "broker: mosquitto pid $($broker.Id) on $($bound -join ' and '):1883; log $brokerLog"

$argv = @()
if ($tunnel) { $argv += $tunnel }
$argv += $forward

# Ctrl-C reaches Python through the console process group; there is no exec here.
Push-Location (Join-Path $repo "laptop")  # flat imports, so run from its own dir
try {
    & $python "pipeline.py" @argv
    $code = $LASTEXITCODE
} finally {
    Pop-Location
    if (-not $broker.HasExited) { Stop-Process -Id $broker.Id -ErrorAction SilentlyContinue }
    Write-Host "broker stopped"
}
exit $code
