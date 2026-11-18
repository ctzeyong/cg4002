# Board half of the pipeline. Pane 2 of two; pane 1 is run_pipeline.ps1.
#
# A wrapper, so both panes start the same way. The work is in run_board.py,
# which authenticates through asyncssh and therefore needs no password.
# run_board_sshexe.ps1 is the ssh.exe version, which cannot be handed one and
# always prompts -- keep it for narrowing down a failure.
#
#   .\run_board.ps1                  # start the client and the engine, or re-attach if running
#   .\run_board.ps1 --sync           # re-copy ultra96\*.py and the engine, then restart both
#   .\run_board.ps1 --stop           # kill both and exit
#   .\run_board.ps1 -v --event-every 2
#
# Starting it stops the laptop-broker board client first: the laptop keeps one client.
#
# The client and the engine run detached and this pane only tails the client's log.
# Ctrl-C here stops both. Closing the window leaves them running.

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
