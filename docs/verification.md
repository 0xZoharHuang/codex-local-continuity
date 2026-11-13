# Verification

## Run with synthetic data

From the repository root:

```sh
PYTHONPATH=src /usr/bin/python3 -B -m unittest discover -v -s tests -p 'test_*.py'
/usr/bin/swiftc -swift-version 6 -warnings-as-errors -typecheck src/Watcher.swift
```

The unit and transaction tests use temporary native JSON and SQLite fixtures. They cover independently edited project views, conflicting edits, an incomplete or newly enrolled profile, explicit ungrouping, and a remote host hint on an otherwise local-looking thread. Input tests cover double sends, overlapping caches at first enrollment, unsupported rich content, remaining capacity and a second pass with no changes. Transaction tests interrupt the worker on either side of replacement, reopen the app during a proposed write, and exercise restore after related and unrelated later edits.

In an interactive macOS GUI session you can also run:

```sh
/usr/bin/python3 -B tests/macos_watcher_smoke.py
```

The smoke test compiles a temporary listener and an independent fixture application. The fixture exits normally. A stub worker records whether the old PID has gone away and how many times the exit was delivered. It never quits the real desktop app. Headless CI runs the fixture suite and compiles the listener; the GUI smoke test needs a logged-in Aqua session.

## What has been observed

On macOS 15.5 / Apple Silicon with desktop build 26.1103.60933 (25894), a local project catalog and its local folders were present while the active native Projects section was empty. A backed-up **one-time** offline restoration made the original groups visible in the official sidebar after restart. A read-only sample could page turn summaries from local chats created under other identities. Neither observation proves access to every chat, attachment or connected service.

The **recurring helper** has been tested on synthetic fixtures and a private copy of the native layout. Its project and recall changes were reversible in the tested scenarios. The live listener has been installed and observed running. At initial publication, a complete natural A → B → A sign-in with a physical ↑ keypress and this recurring helper has not been verified. The one-time restoration and the independent GUI smoke test are different experiments.

## End-to-end acceptance on a trusted Mac

1. Select two authorized profiles. Use the same Codex mode, host, project directory and local thread for both. Start with the read-only preview. Record which chat is explicitly ungrouped.
2. In A, send a harmless plain-text marker in the test thread. Switch normally to B, quit the desktop app, then reopen it. Check the original project names and IDs, the ungrouped chat, and ↑ in the empty composer. Allow for B's existing entries on the first import.
3. Send a different marker in B and repeat the trip to A. Verify that new input returns to A in the same thread, that a different thread does not recall either marker, and that drafts and pending submissions have not moved.
4. With no further changes, another quit should leave the native file alone. Reopening immediately may defer the sync. Check `status`.
5. For plugin comparisons, distinguish **installed**, **available here**, and **connected**. For schedules, compare ID, target, enabled/paused state and the next *natural* run. Do not force a job just to check its visibility.

## Exercise a policy change

Use disposable fixtures for changes you would not want to make on real profiles. The test suite checks the behavior at these boundaries:

- In `folders`, register a project on A. B receives the folder and its place in the list. B's explicit chat assignments, exclusions and sidebar preference remain B's own. Folder-only sync works even when the test database has no chat table.
- In `history`, append a harmless query. B can recall it even when the fixture has no project tables. Project grouping is not inspected for that operation.
- Narrow the group, pause, and then re-enable the earlier choices. Policy changes do not write the native file. Old recovery points cannot be applied to new participants, including after a round trip through the same settings. Pause can operate without opening the native database.
- Compare synthetic plugin/schedule inventories across two identities. A status change, target change, missing definition and missing data are distinguishable. The comparison does not exercise any plugin or send a scheduled prompt.

## Storage and limits

The tested layout uses a local renderer state file and the v5 thread/project SQLite index. The worker opens the index read-only and preserves the original thread IDs. An eligible recall scope needs a local user-facing thread and a regular local session file in the supported location. Unknown schemas, foreign host hints and cloud-backed project definitions are outside the supported set.

The writer checks for an active desktop process, singleton state and concurrent edits before replacing the JSON. The desktop app does not take this helper's lock; an unrelated writer can still race the last check. Recovery uses fingerprints and refuses an ambiguous partial write. An unrecognized format or custom file protection is a reason to defer. If reporting a problem, give the operating system, desktop build, helper commit, status code and reproduction steps with a disposable test chat. Keep prompt text, local paths, identity labels and state files private.

## Where to report a finding

For a problem with this helper, use the [multi-profile issue form](https://github.com/0xZoharHuang/codex-local-continuity/issues/new?template=account-switch.yml). For a reproducible desktop behavior independent of the helper, first check the [upstream feature request](https://github.com/openai/codex/issues/60337) and [project visibility report](https://github.com/openai/codex/issues/60364). Add the build, whether the issue happens on A → B or on the return to A, which native view was checked, and the expected choice. Say explicitly when the observation was on a fixture or read from disk. The [upstream contribution guide](https://github.com/openai/codex/blob/main/docs/contributing.md) welcomes new evidence on existing issues. Never attach a real global-state file to demonstrate the problem.

## Desktop storage migration in build 26605

Desktop 26.1106.51428 migrates the old account renderer container into shared local project state and per-owner settings. Its account switch clears live composer history. Beta.1 assumed the older wrapper and recorded a KeyError after this upgrade. The observed installation has two successful older writes, followed by the format error.

The updated adapter identifies the two formats. On the migrated format it leaves the native shared project registry to the desktop. It watches persisted JSON changes to checkpoint eligible sent text in the helper's private scope. Native writes still require a complete app exit and a verified destination owner. A same-owner startup preserves the restored live cache according to the installed code path; a real A → B → A keyboard check remains pending.

The adapter can seed missing snapshots from eligible pre-change input fragments in its own compatible private recovery points. Such fragments are incomplete and have no exact cross-account ordering. Text already cleared before a checkpoint and absent from those recovery points cannot be recreated by this helper. The watcher is event-driven with a 0.5-second debounce, a one-second scheduling bound per burst and serialized workers. An immediate sign-out before persisted text is captured can still lose that latest cache entry.

Current source tests simulate A's persisted inputs, a switch that clears the live cache, B's normal exit, restoration, no-op replay, an interrupted transaction, quick reopen, new-format diagnostics, and refusal to apply a legacy recovery point across the migration. An independent Aqua fixture tests file replacement, in-place save, directory replacement, unrelated SQLite writes and exit while a checkpoint worker is busy.

## Repeated switches, rapid relaunch, and later upgrades

The follow-up suite runs 65 bounded A/B/C cycles, empty-cache observations followed by repeated equal text, temporary identity disappearance and return, and narrowing the permitted group. A formerly verified profile can retain a private checkpoint, while only profiles verified in the current native state can source or receive a merge.

The actual executable worker is also tested in a subprocess. Script and imported adapter now share one exception class and sentinel, so a concurrently replaced snapshot is classified as a transient defer instead of a misleading generic failure. A later successful unchanged capture clears that obsolete capture status.

The GUI smoke test uses disposable applications to cover relaunch before sync, a same-path application package replacement, a capture that exits with an error, and an exit that arrives before the debounce timer. A deliberately stuck capture is terminated at its 30-second limit, freeing the queued exit sync. Writeback workers are never subject to that capture timeout.

Immediately before native replacement, the writer rereads the file and exact consent generation, then checks that the app is still offline. A changed consent also prevents a no-change merge from storing an old wider checkpoint under new settings. Restoring a bucket revalidates that the thread still belongs to the supported local inventory. A new ownership-container version or a downgrade with an incompatible pending receipt produces a named refusal and preserves the native file and recovery evidence.

These are bounded checks against observed layouts. An undocumented future desktop release can still require a new adapter. A same-content send whose preceding reset was never saved or observed remains indistinguishable from the old snapshot.
