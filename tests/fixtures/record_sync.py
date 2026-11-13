"""This test-only stand-in for sync.py never imports the real worker."""
import json
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time


root = Path(__file__).resolve().parent
assert sys.argv[1:] in (["sync"], ["capture"])
action = sys.argv[1]
trigger = os.environ["CODEX_CONTINUITY_TRIGGER"]
started = time.time()
lock = root / "worker.lock"
lock_handle = lock.open("a")
fcntl.flock(lock_handle,fcntl.LOCK_EX | fcntl.LOCK_NB)
record = {"trigger": trigger, "pid": None, "action": action, "started": started}
exit_code = 0
if trigger == "termination":
    assert action == "sync"
    pid = int(os.environ["CODEX_CONTINUITY_EXIT_PID"])
    assert pid > 1
    try:
        os.kill(pid, 0)
        pid_alive = True
    except ProcessLookupError:
        pid_alive = False
    ps = subprocess.run(["/bin/ps", "-ww", "-p", str(pid), "-o", "comm="],
                        capture_output=True, text=True, check=False)
    target = root / "WatcherFixture.app/Contents/MacOS/Fixture"
    quitting = float((root / f"quitting-{pid}").read_text())
    record.update(pid=pid, pid_alive=pid_alive, executable_present=ps.stdout.strip() == str(target),
                  elapsed_ms=round((started - quitting) * 1000, 1))
elif action == "capture":
    assert trigger == "native-state-changed" and "CODEX_CONTINUITY_EXIT_PID" not in os.environ
    with (root / "capture-started").open("a") as file:
        print(started, file=file)
    if (root/"hang-capture-once").exists():
        (root/"hang-capture-once").unlink()
        (root/"capture-hung").write_text(str(os.getpid()))
        while True:
            time.sleep(60)
    while (root / "hold-capture").exists() and time.time() - started < 8:
        time.sleep(0.02)
    try:
        native = json.loads((root/"native/.codex-global-state.json").read_text())
        record["sequence"] = native.get("sequence")
    except (FileNotFoundError, ValueError):
        record["sequence"] = None
    if (root/"fail-capture-once").exists():
        (root/"fail-capture-once").unlink()
        record["simulated_failure"] = True
        exit_code = 2
else:
    assert action == "sync"
    assert trigger == "startup-offline" and "CODEX_CONTINUITY_EXIT_PID" not in os.environ
if action == "sync" and (root/"simulate-offline-write").exists():
    record["sequence_before_sync"] = json.loads((root/"native/.codex-global-state.json").read_text()).get("sequence")
    temporary = root/"native/staged-json"
    temporary.write_text('{"sequence":999}')
    os.replace(temporary,root/"native/.codex-global-state.json")
record["finished"] = time.time()
with (root / "events.jsonl").open("a") as file:
    print(json.dumps(record), file=file)
lock_handle.close()
sys.exit(exit_code)
