# The pipeline in one Windows Terminal window. The dashboard is on the left, and the
# relay (run_pipeline.ps1) and board client (run_board.ps1) logs are stacked on the right.
#
#   .\run_all.ps1                          # start all three
#   .\run_all.ps1 --sync --gesture-flag    # arguments go to run_board.ps1
#
# Ctrl-C in the dashboard stops all three, each one as if Ctrl-C had been pressed in
# its own pane. Closing the dashboard pane stops only the dashboard.
# Each pane runs the same command you would type yourself, so any pane can be
# closed and started again on its own.
# No param block on purpose: it would bind -v to -Verbose instead of forwarding it.

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $here

# Wait mode, which the board and dashboard panes run before they start.
#   .\run_all.ps1 --wait-for <log name> <text> <launch time in UTC ticks> [seconds]
# It returns once a log with that name, made since the launch, contains the text.
# After the timeout it returns anyway, so a failed login never leaves a pane waiting.
if ($args.Count -ge 4 -and $args[0] -eq "--wait-for") {
    $name, $text = $args[1], $args[2]
    # a second early, so a log made in the same second as the launch still counts
    $since = [DateTime]::new([int64]$args[3], [DateTimeKind]::Utc).AddSeconds(-1)
    $seconds = if ($args.Count -ge 5) { [double]$args[4] } else { 30 }
    # e.g. run_board_124951Z.log, but not run_board_a_124951Z.log
    $pattern = "^$([regex]::Escape($name))_\d{6}Z(-\d+)?\.log$"
    $deadline = (Get-Date).AddSeconds($seconds)
    Write-Host "Waiting for $name to log '$text' (at most $seconds s)"
    while ((Get-Date) -lt $deadline) {
        $logs = Get-ChildItem -Path (Join-Path $repo "logs\*\$($name)_*.log") -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match $pattern -and $_.CreationTimeUtc -ge $since }
        foreach ($log in $logs) {
            if (Select-String -LiteralPath $log.FullName -SimpleMatch $text -Quiet -ErrorAction SilentlyContinue) {
                exit 0
            }
        }
        Start-Sleep -Milliseconds 200
    }
    Write-Host "No '$text' after $seconds s, so starting anyway"
    exit 0
}

if (-not (Get-Command wt.exe -ErrorAction SilentlyContinue)) {
    Write-Error "no Windows Terminal (wt.exe), so start run_pipeline.ps1 and run_board.ps1 by hand"
    exit 1
}

# PowerShell parses `1,2` into an array, which would reach Python as two args.
$forward = @()
foreach ($a in $args) {
    if ($a -is [array]) { $forward += ($a -join ",") } else { $forward += [string]$a }
}
$board = (@(".\scripts\run_board.ps1") + $forward) -join " "

# Each pane starts once the one before it has logged in, since the Windows ssh-agent
# turns away two logins that ask at the same moment.
$launch = [DateTime]::UtcNow.Ticks
$waitForRelay = ".\scripts\run_all.ps1 --wait-for pipeline_board_broker 'tunnel open' $launch 30"
$waitForBoard = ".\scripts\run_all.ps1 --wait-for run_board 'connected to the board' $launch 45"

# wt splits its own command line on ";", so all three panes go in one string.
# A ";" meant for PowerShell inside a pane is written "\;".
# focus-pane -t 1 picks the relay pane, so the board splits it and not the dashboard.
# --stop-pipeline makes Ctrl-C in the dashboard stop the relay and the board client too.
# It finds them by $launch in their pane's command line, so it never stops another window's.
$panes = @(
    "-w new --maximized new-tab --title Dashboard -d `"$repo`" powershell -NoExit -Command $waitForBoard \; .\.venv\Scripts\python.exe .\scripts\dashboard --stop-pipeline $launch",
    "split-pane -V --size 0.45 --title Relay -d `"$repo`" powershell -NoExit -Command .\scripts\run_pipeline.ps1 \; # run $launch",
    "focus-pane -t 1",
    "split-pane -H --title Board -d `"$repo`" powershell -NoExit -Command $waitForRelay \; $board"
)
Start-Process wt.exe -ArgumentList ($panes -join " ; ")
