# Configuration A on a phone-hotspot uplink, board half. Pane 2 of two; pane 1 is
# run_pipeline_a_hotspot.ps1.
#
# A wrapper, so both panes start the same way. The work is in run_board_a_hotspot.py,
# which reaches the board over Tailscale with the laptop's key: no VPN, no password.
#
#   .\run_board_a_hotspot.ps1 --sync --gesture-flag   # copy, (re)start, show its log
#   .\run_board_a_hotspot.ps1                  # start it, or re-attach if already running
#   .\run_board_a_hotspot.ps1 --stop           # kill it and exit
#   .\run_board_a_hotspot.ps1 --fetch-logs     # copy today's (UTC) board logs to logs\board\
#
# Starting it stops configuration B's board client first: the laptop keeps one client.
# Ctrl-C here stops the board client.

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $here
$python = Join-Path $repo ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Error "no interpreter at $python"
    exit 1
}

# PowerShell parses `1,2` into an array, which would arrive as two arguments.
$forward = @()
foreach ($a in $args) {
    if ($a -is [array]) { $forward += ($a -join ",") } else { $forward += [string]$a }
}

& $python (Join-Path $here "run_board_a_hotspot.py") @forward
exit $LASTEXITCODE
