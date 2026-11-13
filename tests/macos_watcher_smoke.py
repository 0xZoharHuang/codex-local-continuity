#!/usr/bin/env python3
"""Run from an unlocked macOS GUI login with Xcode Command Line Tools:

    python3 tests/macos_watcher_smoke.py

CI: use a macOS runner with a working Aqua login and Launch Services. Run this
as a separate step from unittest discovery. A headless runner cannot validate
NSWorkspace/Launch Services and should run the ordinary unit tests instead.
"""
import contextlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import uuid


TESTS = Path(__file__).resolve().parent


def wait_for(predicate, description, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.025)
    raise AssertionError(f"Timed out waiting for {description}")


def compile_swift(source, binary, cache):
    subprocess.run(["/usr/bin/swiftc", "-swift-version", "6", "-warnings-as-errors",
                    "-module-cache-path", str(cache), "-o", str(binary), str(source)], check=True)


def build(root):
    bundle = root / "WatcherFixture.app"
    bundle_id = "local.test.codex-continuity-" + uuid.uuid4().hex
    executable = bundle / "Contents/MacOS/Fixture"
    executable.parent.mkdir(parents=True)
    info = {"CFBundleIdentifier": bundle_id, "CFBundleExecutable": "Fixture",
            "CFBundleName": "WatcherFixture", "CFBundlePackageType": "APPL",
            "CFBundleVersion": "1", "LSUIElement": True}
    (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    cache = root / "module-cache"
    compile_swift(TESTS / "fixtures/WatcherFixture.swift", executable, cache)
    source = (TESTS.parent / "src/Watcher.swift").read_text()
    # Substitute only the target declaration in this disposable source copy.
    # Observer, PID checks and task launching are the production implementation.
    binding = "private let target = AppTarget.codex"
    if source.count(binding) != 1:
        raise AssertionError("Watcher target changed; update the test adapter")
    test_target = (f"private let target = AppTarget(bundleID: {json.dumps(bundle_id)}, "
                   f"bundles: [URL(fileURLWithPath: {json.dumps(str(bundle), ensure_ascii=False)})], "
                   'executables: ["Fixture"])')
    source = source.replace(binding, test_target)
    state_binding = ('private let stateURL = FileManager.default.homeDirectoryForCurrentUser\n'
                     '        .appendingPathComponent(".codex/.codex-global-state.json")')
    if source.count(state_binding) != 1:
        raise AssertionError("Native state path changed; update the test adapter")
    source = source.replace(state_binding, 'private let stateURL = URL(fileURLWithPath: ' +
                            json.dumps(str(root/"native/.codex-global-state.json"), ensure_ascii=False) + ')')
    (root/"native").mkdir()
    write_state(root,0)
    test_watcher = root / "Watcher.swift"
    test_watcher.write_text(source)
    compile_swift(test_watcher, root / "Watcher", cache)
    shutil.copyfile(TESTS / "fixtures/record_sync.py", root / "sync.py")
    return bundle


def events(root):
    file = root / "events.jsonl"
    return [json.loads(line) for line in file.read_text().splitlines()] if file.exists() else []


def write_state(root, sequence):
    stage = root/"native/staged-json"
    stage.write_text(json.dumps({"sequence":sequence}))
    os.replace(stage,root/"native/.codex-global-state.json")


def captures(root):
    return [event for event in events(root) if event["action"] == "capture"]


def captured(root, sequence):
    return [event for event in captures(root) if event.get("sequence") == sequence]


def capture_starts(root):
    path = root/"capture-started"
    return len(path.read_text().splitlines()) if path.exists() else 0


def verify_captures(root):
    wait_for(lambda: captured(root,0), "startup capture of current file")
    time.sleep(0.65)
    count = len(captures(root))
    for sequence in range(1,11):
        write_state(root,sequence)
        time.sleep(0.015)
    wait_for(lambda: captured(root,10), "coalesced atomic replacements")
    time.sleep(0.65)
    assert len(captures(root)) == count+1, "short burst produced redundant captures"
    # The file source must be rearmed on the new inode, including in-place writes.
    (root/"native/.codex-global-state.json").write_text('{"sequence":11}')
    wait_for(lambda: captured(root,11), "in-place update after atomic replacements")
    time.sleep(0.65)
    count = len(captures(root))
    for _ in range(20):
        noise = root/"native/unrelated-sqlite-wal"
        noise.write_bytes(b"unrelated")
        noise.unlink()
    time.sleep(0.75)
    assert len(captures(root)) == count, "unrelated directory writes invoked the worker"
    # Even a continuing stream produces a capture, bounded by the 1s debounce.
    start = time.time()
    for sequence in range(12,22):
        write_state(root,sequence)
        time.sleep(0.13)
    timed = [event for event in captures(root)[count:] if event["started"]-start < 1.25]
    assert timed, "continuing updates starved the bounded debounce"
    wait_for(lambda: captured(root,21), "last update in continuous burst")
    time.sleep(0.65)
    moved = root/"previous-native"
    (root/"native").rename(moved)
    time.sleep(0.2)
    (root/"native").mkdir()
    write_state(root,24)
    wait_for(lambda: captured(root,24), "parent directory replacement and fallback")
    time.sleep(0.65)


@contextlib.contextmanager
def watcher(root):
    process = subprocess.Popen([str(root / "Watcher")], stdin=subprocess.DEVNULL)
    try:
        yield
    finally:
        process.terminate()
        process.wait(timeout=5)


@contextlib.contextmanager
def fixture(bundle):
    root = bundle.parent
    earlier = set(root.glob("launched-*"))
    subprocess.run(["/usr/bin/open", "-g", "-j", str(bundle)], check=True, timeout=5)
    launches = wait_for(lambda: set(root.glob("launched-*")) - earlier, "fixture launch")
    assert len(launches) == 1
    pid = int(launches.pop().name.split("-")[-1])
    try:
        yield pid
    finally:
        (root / f"quit-{pid}").touch()
        wait_for(lambda: (root / f"quitting-{pid}").exists(), "fixture's own normal termination")


def verify_exit(root, pid):
    def matching():
        return [record for record in events(root) if record["pid"] == pid]
    wait_for(matching, f"worker notification for fixture PID {pid}")
    # Allow the duplicate Launch Services or kernel notification to be delivered.
    time.sleep(0.35)
    observed = matching()
    assert len(observed) == 1, f"{len(observed)} worker calls for one exit"
    assert not observed[0]["pid_alive"] and not observed[0]["executable_present"], observed
    return observed[0]["elapsed_ms"]


def run(root):
    bundle = build(root)
    timings = []
    with watcher(root):
        wait_for(lambda: events(root), "startup-offline notification")
        with fixture(bundle) as pid:
            verify_captures(root)
            # Hold the capture worker while the app exits. The exit sync must
            # run next, without a second worker or a pending capture overtaking it.
            (root/"hold-capture").touch()
            started = capture_starts(root)
            blocked_since = time.time()
            write_state(root,22)
            wait_for(lambda: capture_starts(root)>started, "held capture worker")
            write_state(root,23)
            time.sleep(0.6)
            (root/f"quit-{pid}").touch()
            wait_for(lambda: (root/f"quitting-{pid}").exists(), "exit during busy capture")
            time.sleep(0.2)
            (root/"simulate-offline-write").touch()
            (root/"hold-capture").unlink()
            timings.append(verify_exit(root,pid))
        time.sleep(0.7)
        assert events(root)[-1]["action"] == "sync", "offline native write scheduled another capture"
        recent = [e["action"] for e in events(root) if e["started"]>blocked_since]
        assert recent[-2:] == ["capture","sync"]
        with fixture(bundle) as pid:
            wait_for(lambda: captured(root,999), "capture on app relaunch")
        timings.append(verify_exit(root,pid))
    before = capture_starts(root)
    with fixture(bundle) as pid, watcher(root):
        # The app must still be running while this second watcher discovers it.
        wait_for(lambda: capture_starts(root)>before, "capture of already-running app")
        (root / f"quit-{pid}").touch()
        timings.append(verify_exit(root, pid))
    assert [r["trigger"] for r in events(root) if r["action"]=="sync"] == ["startup-offline"] + ["termination"] * 3
    calls = events(root)
    assert all(a["finished"]<=b["started"] for a,b in zip(calls,calls[1:])), "workers overlapped"
    print("PASS: native state capture, rename rearming, burst coalescing, bounded debounce, "
          "directory noise filtering, sync priority and 3 natural exits. Exit elapsed ms:",timings)


if __name__ == "__main__":
    if sys.platform != "darwin":
        sys.exit("This smoke test requires a macOS Aqua session.")
    with tempfile.TemporaryDirectory(prefix="codex-continuity-smoke-") as directory:
        run(Path(directory).resolve())
