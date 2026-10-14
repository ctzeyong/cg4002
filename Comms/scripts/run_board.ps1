# Configuration B, board half. Pane 2 of two; pane 1 is run_pipeline.ps1.
#
# A wrapper, so both panes start the same way. The work is in run_board.py,
# which authenticates through asyncssh and therefore needs no password.
# run_board_sshexe.ps1 is the ssh.exe version, which cannot be handed one and
# always prompts -- keep it for bisecting. notes/tunnelled_visualiser.md.
#
#   .\run_board.ps1                  # start it, or re-attach if already running
#   .\run_board.ps1 --sync           # re-copy ultra96\*.py, then restart it
#   .\run_board.ps1 --stop           # kill it and exit
#   .\run_board.ps1 -v --event-every 2
#
# Starting it stops configuration A's board client first: the laptop keeps one client.
#
# The client runs detached and this pane only tails its log, so Ctrl-C here does
# not take the uplink down.

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

& $python (Join-Path $here "run_board.py") @forward
exit $LASTEXITCODE
