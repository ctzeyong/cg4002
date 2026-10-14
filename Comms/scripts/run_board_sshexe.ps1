# FALLBACK -- use run_board.ps1 instead. This drives ssh.exe, which takes a
# password only from a terminal, so it prompts every run and twice with --sync.
# It exists to answer one question when run_board.ps1 fails: is the SSH path
# broken, or the asyncssh launcher? Same role as the raw -R command in
# ultra96_setup.md. Do not "simplify" the two into one.
#
# Configuration B, board half: start ultra96/tcp_client_mqtt.py over the jump
# host. Pane 2 of two. Auth and the preflight checks: notes/ultra96_setup.md,
# notes/tunnelled_visualiser.md.
#
#   .\run_board_sshexe.ps1                  # start it, or re-attach
#   .\run_board_sshexe.ps1 --sync           # re-copy ultra96\*.py, then restart
#   .\run_board_sshexe.ps1 --stop           # kill it and exit
#   .\run_board_sshexe.ps1 -v --event-every 2
#
# The client runs detached and this pane only tails its log, so Ctrl-C here does
# not take the uplink down. Everything but --sync and --stop is passed through to
# tcp_client_mqtt.py. One jump-host password per session; --sync opens a second
# session and so asks twice.

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $here

# By path, not by name: C:\msys64\usr\bin is on PATH and its ssh sees no agent.
$ssh = $env:B16_SSH
if (-not $ssh) { $ssh = Join-Path $env:SystemRoot "System32\OpenSSH\ssh.exe" }
$scp = $env:B16_SCP
if (-not $scp) { $scp = Join-Path $env:SystemRoot "System32\OpenSSH\scp.exe" }
if (-not (Test-Path $ssh)) {
    Write-Error "no ssh.exe at $ssh (set B16_SSH)"
    exit 1
}

# The same .env keys tunnel.py reads, so no NUSNET id or board number is committed.
$envFile = Join-Path $repo ".env"
if (-not (Test-Path $envFile)) {
    Write-Error "missing $envFile -- copy .env.example"
    exit 1
}
$settings = @{}
foreach ($line in Get-Content $envFile) {
    $text = $line.Trim()
    if ($text -eq "" -or $text.StartsWith("#")) { continue }
    $split = $text.IndexOf("=")
    if ($split -lt 1) { continue }
    $settings[$text.Substring(0, $split).Trim()] = $text.Substring($split + 1).Trim().Trim('"', "'")
}
function Get-Setting($key, $fallback) {
    if ($settings.ContainsKey($key) -and $settings[$key]) { return $settings[$key] }
    return $fallback
}
$jumpUser  = Get-Setting JUMP_USER  ""
$jumpHost  = Get-Setting JUMP_HOST  "stujump.comp.nus.edu.sg"
$boardUser = Get-Setting BOARD_USER "xilinx"
$boardHost = Get-Setting BOARD_HOST ""
$port      = Get-Setting TUNNEL_PORT "8888"

foreach ($key in @("JUMP_USER", "BOARD_HOST")) {
    if (-not (Get-Setting $key "")) {
        Write-Error "missing $key in $envFile"
        exit 1
    }
}

$jumpSpec  = "{0}@{1}" -f $jumpUser, $jumpHost
$boardSpec = "{0}@{1}" -f $boardUser, $boardHost

$passthru = @()
foreach ($a in $args) {
    if ($a -is [array]) { $passthru += ($a -join ",") } else { $passthru += [string]$a }
}
$sync = $false
$stop = $false
$clientArgs = @()
foreach ($a in $passthru) {
    if ($a -eq "--sync") { $sync = $true }
    elseif ($a -eq "--stop") { $stop = $true }
    else { $clientArgs += $a }
}

# Hosts on camera are fine, the NUSNET id is not, as in TunnelConfig.describe.
Write-Output ("Running TCP client and MQTT client on Ultra96: {0}, tunnel port {2}" -f $boardHost, $jumpHost, $port)

# The board runs whatever was last copied. The whole glob, never a hand list.
if ($sync) {
    Write-Output "syncing ultra96\*.py to ~/comms/ (asks for the jump password again)"
    Push-Location (Join-Path $repo "ultra96")
    try {
        # PowerShell does not expand globs for native commands, so do it here.
        $files = @(Get-ChildItem -Filter *.py | ForEach-Object { $_.Name })
        & $scp "-J" $jumpSpec "--" @files ("{0}:comms/" -f $boardSpec)
        $copied = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($copied -ne 0) {
        Write-Error "scp failed; the board still has the previous code"
        exit 1
    }
}

# nohup, not a foreground child: the client would otherwise take a SIGHUP when
# this SSH session dies and leave the relay talking to nothing. It already writes
# a per-run log, so the pane tails that instead of owning the process.
# Single-quoted PowerShell strings below, so $latest and $(...) reach the shell.
$launch = (
    'cap=~/comms/logs/console_$(date -u +%Y%m%dT%H%M%SZ).log; ' +
    ("nohup ~/comms-venv/bin/python ~/comms/tcp_client_mqtt.py --host 127.0.0.1 --port {0} {1} " `
        -f $port, ($clientArgs -join " ")).Replace("  ", " ") +
    '> "$cap" 2>&1 &'
)

$remote = @()
if ($sync -or $stop) {
    # A --sync that attached instead would leave the OLD process running the code
    # you just replaced, which is the stale-board failure wearing a disguise.
    # Anchored at the interpreter: the bare script name matches our own remote
    # shell too, so pkill would kill the shell running it. Real run, 22 Sep.
    $remote += 'pkill -f "^[^ ]*python[0-9.]*[ ]+[^ ]*tcp_client_mqtt\.py" && echo STOPPED: previous client killed'
    $remote += 'sleep 1'
}
if (-not $stop) {
    $remote += "if ! ss -ltn | grep -q '127.0.0.1:$port '; then echo 'WARN: nothing listening on 127.0.0.1:$port -- is the laptop tunnel up?'; fi"
    $remote += "if ! ss -ltn | grep -q ':1883 '; then echo 'WARN: no broker on 1883 -- sudo systemctl start mosquitto'; fi"
    $remote += "ss -ltn | grep ':1883 ' | grep -qv '127.0.0.1\|::1' && echo 'WARN: the broker is bound wider than loopback'"
    # Absolute interpreter path: a venv activated by a script silently is not.
    # The capture holds stdout too, so an import failure lands in this pane
    # instead of vanishing -- that is the "board looks down" case.
    $remote += 'mkdir -p ~/comms/logs'
    # $latest is the file we just created, never the newest on disk: a launch that
    # died would otherwise tail a PREVIOUS run's capture and show stale output as
    # though it were live. Only the attach path may glob, and there it is correct.
    $remote += 'if pgrep -f "^[^ ]*python[0-9.]*[ ]+[^ ]*tcp_client_mqtt\.py" >/dev/null; then echo "ATTACH: client already running"; latest=$(ls -t ~/comms/logs/console_*.log 2>/dev/null | head -1); else ' + $launch + ' sleep 2; echo STARTED; latest="$cap"; fi'
    # A client started by hand has no capture; that is normal, not an error.
    $remote += 'if [ -z "$latest" ]; then latest=$(ls -t ~/comms/logs/*/tcp_client_mqtt_*.log 2>/dev/null | head -1); fi'
    $remote += 'if [ -z "$latest" ]; then echo "ERROR: nothing to tail and no client running"; exit 1; fi'
    $remote += 'echo "tailing $latest -- Ctrl-C leaves the client running, --stop ends it"'
    # tail, not the client: Ctrl-C here must not be what takes the uplink down.
    $remote += 'exec tail -n 200 -f "$latest"'
}
$remoteCmd = $remote -join "; "

# -t for a pty so Ctrl-C reaches tail. The pane is plain, not coloured: the log
# file is deliberately written without ANSI. notes/tunnelled_visualiser.md.
& $ssh "-t" "-J" $jumpSpec $boardSpec $remoteCmd
exit $LASTEXITCODE
