# Configuration B, laptop half: the relay with no MQTT role. Pane 1 of two.
# Bring-up and the traps guarded below: notes/tunnelled_visualiser.md.
#
#   .\run_pipeline.ps1 [--no-tunnel] [-v] [--nodes 1,2] ...
#
# Everything but --no-tunnel is passed through to pipeline_board_broker.py.
# No param block on purpose: it would bind -v to -Verbose instead of forwarding it.

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $here
$python = Join-Path $repo ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Error "no interpreter at $python"
    exit 1
}

# PowerShell parses `1,2` into an array, which would reach Python as two args.
$passthru = @()
foreach ($a in $args) {
    if ($a -is [array]) { $passthru += ($a -join ",") } else { $passthru += [string]$a }
}

# --tunnel by default: under B the tunnel is just another leg on the same loop.
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

Write-Output "Running BLE connections and TCP server with tunneling to Ultra96 (MQTT broker)"

$argv = @()
if ($tunnel) { $argv += $tunnel }
$argv += $forward

# Ctrl-C reaches Python through the console process group; there is no exec here.
Push-Location (Join-Path $repo "laptop")  # flat imports, so run from its own dir
try {
    & $python "pipeline_board_broker.py" @argv
} finally {
    Pop-Location
}
exit $LASTEXITCODE
