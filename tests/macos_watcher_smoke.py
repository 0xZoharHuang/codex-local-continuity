#!/usr/bin/env python3
"""Run from an unlocked macOS GUI login with Xcode Command Line Tools:

    python3 tests/macos_watcher_smoke.py

CI: use a macOS runner with a working Aqua login and Launch Services. Run this
as a separate step from unittest discovery. A headless runner cannot validate
NSWorkspace/Launch Services and should run the ordinary unit tests instead.
"""
import contextlib
import json
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
    test_watcher = root / "Watcher.swift"
    test_watcher.write_text(source)
    compile_swift(test_watcher, root / "Watcher", cache)
    shutil.copyfile(TESTS / "fixtures/record_sync.py", root / "sync.py")
    return bundle


def events(root):
    file = root / "events.jsonl"
    return [json.loads(line) for line in file.read_text().splitlines()] if file.exists() else []


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
        for _ in range(2):
            with fixture(bundle) as pid:
                time.sleep(0.3)
            timings.append(verify_exit(root, pid))
    with fixture(bundle) as pid, watcher(root):
        # The app must still be running while this second watcher discovers it.
        time.sleep(1.0)
        (root / f"quit-{pid}").touch()
        timings.append(verify_exit(root, pid))
    assert [r["trigger"] for r in events(root)] == ["startup-offline"] + ["termination"] * 3
    print(f"PASS: 3 natural exits, one worker call each, all old PIDs gone. Elapsed ms: {timings}")


if __name__ == "__main__":
    if sys.platform != "darwin":
        sys.exit("This smoke test requires a macOS Aqua session.")
    with tempfile.TemporaryDirectory(prefix="codex-continuity-smoke-") as directory:
        run(Path(directory).resolve())
