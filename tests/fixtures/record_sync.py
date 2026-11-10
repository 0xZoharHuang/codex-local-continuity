"""This test-only stand-in for sync.py never imports the real worker."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time


root = Path(__file__).resolve().parent
assert sys.argv[1:] == ["sync"]
trigger = os.environ["CODEX_CONTINUITY_TRIGGER"]
started = time.time()
record = {"trigger": trigger, "pid": None}
if trigger == "termination":
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
else:
    assert trigger == "startup-offline" and "CODEX_CONTINUITY_EXIT_PID" not in os.environ
with (root / "events.jsonl").open("a") as file:
    print(json.dumps(record), file=file)
