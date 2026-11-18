# Laptop-broker setup on a phone-hotspot uplink, laptop half: Mosquitto on this laptop's
# address on the phone's hotspot, the Tailscale tunnel to the board, then the relay.
# Pane 1 of two; pane 2 is run_board_a_hotspot.ps1. Campus runs use run_pipeline_a.ps1.
# This setup was evaluated but is not used in the final system. What it needs is in
# Comms/README.md, section 2.
#
#   .\run_pipeline_a_hotspot.ps1 [-v] [--nodes 1,2] ...
#
# Other arguments go to pipeline.py, except the ones this script sets itself. The broker
# and the tunnel stop when the relay exits. Their logs go to logs\<UTC date>\.
# No param block on purpose: it would bind -v to -Verbose instead of forwarding it.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $here
$python = Join-Path $repo ".venv\Scripts\python.exe"
$mosquitto = "C:\Program Files\Mosquitto\mosquitto.exe"
$template = Join-Path $repo "laptop\mosquitto_hotspot.conf"
$tunnelScript = Join-Path $repo "laptop\tunnel_tailscale.py"
$firewallRule = "B16 MQTT 1883 local subnet"  # the rule's name in Comms/README.md

foreach ($file in @($python, $mosquitto, $template, $tunnelScript)) {
    if (-not (Test-Path $file)) {
        Write-Error "not found: $file"
        exit 1
    }
}

# PowerShell parses `1,2` into an array, which would reach Python as two args.
$forward = @()
foreach ($a in $args) {
    if ($a -is [array]) { $forward += ($a -join ",") } else { $forward += [string]$a }
}
# The tunnel, broker address and TCP port are set here; overriding one would break the others.
foreach ($a in $forward) {
    if ($a -match '^--(tunnel|no-tunnel|tcp-host|tcp-port|host|port|tls)(=|$)') {
        Write-Error "$a is set by this script; run pipeline.py directly to change it"
        exit 1
    }
}
# keystroke_reader uses msvcrt, which reads the console rather than stdin.
if ([Console]::IsInputRedirected) {
    Write-Warning "stdin is redirected, so keypresses will send no CMD packet."
}

# --- Network checks -------------------------------------------------------------------

# The Cisco VPN sends 10.x through NUS, so the phone could not reach the broker here.
if (@(Get-NetAdapter | Where-Object { $_.InterfaceDescription -like "*AnyConnect*" -and $_.Status -eq "Up" }).Count -gt 0) {
    Write-Error "the Cisco VPN is connected: disconnect it. This setup reaches the board over Tailscale."
    exit 1
}
if (@(Get-NetAdapter | Where-Object { $_.InterfaceDescription -like "Tailscale*" -and $_.Status -eq "Up" }).Count -eq 0) {
    Write-Error "Tailscale is not connected: start it (tailscale up) and try again"
    exit 1
}

# The laptop's address on the uplink, which is the address the phone must use.
$uplinkIp = Find-NetRoute -RemoteIPAddress 8.8.8.8 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress } | Select-Object -First 1
if (-not $uplinkIp) {
    Write-Error "no route to the internet: join the phone's hotspot first"
    exit 1
}
$lease = $uplinkIp.IPAddress
$uplink = $uplinkIp.InterfaceAlias
if ($uplinkIp.InterfaceAlias -like "Tailscale*") {
    Write-Error "internet traffic goes through Tailscale (an exit node?); turn that off"
    exit 1
}
# Only a private address: the broker must never listen on a public one.
if ($lease -notmatch '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)') {
    Write-Error "the uplink address $lease is not a private one; is the laptop on the phone's hotspot?"
    exit 1
}

# Never listen on an NUS network: the laptop is not allowed to host servers there.
$ssid = $null
$current = $null
foreach ($line in (netsh wlan show interfaces)) {
    if ($line -match '^\s*Name\s*:\s*(.+?)\s*$') { $current = $Matches[1] }
    elseif ($line -match '^\s*SSID\s*:\s*(.+?)\s*$' -and $current -eq $uplink) { $ssid = $Matches[1] }
}
if ($ssid -match '^(NUS|eduroam)') {
    Write-Error "the laptop is on $ssid. On campus use run_pipeline_a.ps1; this one is for a phone's hotspot."
    exit 1
}
if (-not $ssid) {
    Write-Warning "could not read the Wi-Fi network name for $uplink; check it is the phone's hotspot"
}

# The laptop's own hotspot is not used here, and it would share the Wi-Fi radio.
if (Get-NetIPAddress -IPAddress 192.168.137.1 -ErrorAction SilentlyContinue) {
    Write-Warning "the laptop's Mobile hotspot is on. It is not used here; turn it off so it does not share the radio."
}
$rule = Get-NetFirewallRule -DisplayName $firewallRule -ErrorAction SilentlyContinue
if (-not $rule -or -not ($rule | Where-Object { $_.Enabled -eq "True" })) {
    Write-Warning "no enabled firewall rule '$firewallRule'; the phone may not get through to port 1883"
}

# --- Broker ----------------------------------------------------------------------------

# The service would hold 1883 and answer as the wrong broker, silently.
$service = Get-Service mosquitto -ErrorAction SilentlyContinue
if ($service -and $service.Status -eq "Running") {
    Write-Error "the Mosquitto service is running and holds 1883. In an elevated shell: Stop-Service mosquitto"
    exit 1
}
$holders = @(Get-NetTCPConnection -LocalPort 1883 -State Listen -ErrorAction SilentlyContinue)
if ($holders.Count -gt 0) {
    $ids = $holders.OwningProcess | Sort-Object -Unique
    Write-Error "port 1883 is already held by process $($ids -join ', '). A broker left from an earlier run? Stop-Process -Id <pid>"
    exit 1
}

# A per-run copy of the template with this session's address and a log file. It holds the
# lease and an absolute path, so it lives under logs\, which git ignores.
$now = (Get-Date).ToUniversalTime()
$logDir = Join-Path $repo ("logs\" + $now.ToString("yyyy-MM-dd"))
New-Item -ItemType Directory -Force $logDir | Out-Null
$stem = Join-Path $logDir ("mosquitto_hotspot_" + $now.ToString("HHmmss") + "Z")
$brokerLog = "$stem.log"
$brokerErr = "$stem.err.log"  # only what Mosquitto prints before it reads its config
$brokerConf = "$stem.conf"
$leaseLine = "listener 1883 HOTSPOT_LEASE"
$logLine = "log_dest stdout"
$conf = Get-Content $template
foreach ($expected in @($leaseLine, $logLine)) {
    if (@($conf | Where-Object { $_ -eq $expected }).Count -ne 1) {
        Write-Error "laptop\mosquitto_hotspot.conf no longer has exactly one '$expected' line to replace"
        exit 1
    }
}
$conf | ForEach-Object {
    if ($_ -eq $leaseLine) { "listener 1883 $lease" }
    elseif ($_ -eq $logLine) { "log_dest file $brokerLog" }
    else { $_ }
} | Set-Content -Path $brokerConf -Encoding ascii

$broker = Start-Process -FilePath $mosquitto -ArgumentList "-c", "`"$brokerConf`"" `
    -WorkingDirectory (Join-Path $repo "laptop") -NoNewWindow -PassThru `
    -RedirectStandardOutput "$stem.stdout.log" -RedirectStandardError $brokerErr

$tunnel = $null
$code = 1
# Everything after the broker starts is inside try, so Ctrl-C or an early exit still stops it.
try {
    # Both listeners, or nothing: without the lease one the phone cannot connect.
    $deadline = (Get-Date).AddSeconds(5)
    do {
        Start-Sleep -Milliseconds 200
        $bound = @(Get-NetTCPConnection -LocalPort 1883 -State Listen -ErrorAction SilentlyContinue |
            Where-Object { $_.OwningProcess -eq $broker.Id } |
            ForEach-Object { $_.LocalAddress } | Sort-Object -Unique)
        $ready = ($bound -contains "127.0.0.1") -and ($bound -contains $lease)
    } until ($ready -or $broker.HasExited -or ((Get-Date) -gt $deadline))
    if (-not $ready) {
        Write-Error "Mosquitto did not bind 127.0.0.1 and $lease (got: $($bound -join ', ')). See $brokerErr"
        Get-Content $brokerErr, $brokerLog -ErrorAction SilentlyContinue | Select-Object -Last 10
        exit 1
    }
    Write-Host "broker: mosquitto pid $($broker.Id) on $($bound -join ' and '):1883; log $brokerLog"

    # Its own process, sharing this window, so its lines show here beside the relay's.
    $tunnel = Start-Process -FilePath $python -ArgumentList "`"$tunnelScript`"" `
        -WorkingDirectory (Join-Path $repo "laptop") -NoNewWindow -PassThru
    Start-Sleep -Seconds 2
    if ($tunnel.HasExited) {
        Write-Error "the Tailscale tunnel exited at once (code $($tunnel.ExitCode)); its message is above"
        exit 1
    }
    Write-Host ""
    Write-Host "phone: on the '$ssid' hotspot, set the visualiser's Host to $lease (port 1883)"
    Write-Host ""

    # Ctrl-C reaches Python through the console process group; there is no exec here.
    Push-Location (Join-Path $repo "laptop")  # flat imports, so run from its own dir
    try {
        & $python "pipeline.py" @forward
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
} finally {
    if ($tunnel -and -not $tunnel.HasExited) { Stop-Process -Id $tunnel.Id -ErrorAction SilentlyContinue }
    if (-not $broker.HasExited) { Stop-Process -Id $broker.Id -ErrorAction SilentlyContinue }
    Write-Host "tunnel and broker stopped"
}
exit $code
