#!/usr/bin/env bash
# Keep the game engine running on the board, and restart it after a crash.
# run_board.py starts and stops it. Any extra arguments go to game_engine.py.
#
# The wait between restarts is 2 s, doubling after each short run up to 60 s,
# and back to 2 s after a run of 60 s or more (as ble_client.run_node).
# Exit code 3 means the engine fails the same way on every start (a bad
# rules.json, say), so the loop stops instead of retrying.

here=$(dirname "$(readlink -f "$0")")
python=~/comms-venv/bin/python
min_wait=2
max_wait=60
good_run=60

log() {
    echo "$(date -u '+%Y-%m-%d %H:%M:%S,%3N')Z [run_engine] $*"
}

if [ ! -f "$here/game_engine.py" ]; then
    log "no game_engine.py in $here, so not starting"
    exit 1
fi

wait_s=$min_wait
while true; do
    started=$(date +%s)
    log "starting the engine"
    "$python" "$here/game_engine.py" --host 127.0.0.1 --port 1883 --core-dir "$here/engine" "$@"
    code=$?
    ran=$(( $(date +%s) - started ))

    if [ "$code" -eq 3 ]; then
        log "engine exited with code 3 after ${ran}s, which repeats on every start, so not restarting"
        exit 3
    fi
    if [ "$ran" -ge "$good_run" ]; then
        wait_s=$min_wait
    fi
    log "engine exited with code $code after ${ran}s, restarting in ${wait_s}s"
    sleep "$wait_s"
    if [ "$ran" -lt "$good_run" ]; then
        wait_s=$(( wait_s * 2 > max_wait ? max_wait : wait_s * 2 ))
    fi
done
