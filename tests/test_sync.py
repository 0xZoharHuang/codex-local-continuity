"""Integration checks using synthetic native JSON and a private SQLite fixture."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

import sync


THREAD = "10000000-0000-4000-8000-000000000001"
OTHER_THREAD = "10000000-0000-4000-8000-000000000002"
PROMPT = "Please inspect the sample weather chart."
PRIVATE = "Unsubmitted sample for the privacy assertion."


def owner(user, account):
    # Independent reconstruction of Electron's owner, not a mocked identity.
    encoded = json.dumps([user, account], ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class PowerLoss(BaseException):
    pass


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        work = Path(self.temp.name).resolve()
        self.codex = work / "synthetic-codex"
        self.root = work / "continuity"
        self.store = sync.Store(self.root, self.codex, is_offline=lambda: True)
        for folder in (self.codex, self.root, self.store.data, self.store.backups):
            folder.mkdir(mode=0o700)
        self.live = self.store.live
        self.source_id = owner("source", "workspace-a")
        self.target_id = owner("target", "workspace-b")
        self.host = "local:" + str(self.codex)
        project = {"id":"legacy", "name":"Here", "rootPaths":["/fixture/source"],
                   "createdAt":100, "updatedAt":200}
        self.initial = {
            sync.SCOPES: {
                "owner": self.source_id,
                "inactive": {self.target_id: {
                    "globals": {},
                    "atoms": {"local-document-identity": {"userId":"target", "accountId":"workspace-b"}},
                }},
            },
            sync.ATOMS: {
                "local-document-identity": {"userId":"source", "accountId":"workspace-a"},
                "prompt-history": {THREAD:[PROMPT]},
                "composer-prompt-drafts-v2": {THREAD:PRIVATE},
            },
            "local-projects": {"legacy":project},
            "app-server-project-id-by-legacy-project-id-by-host": {self.host:{"legacy":"native"}},
            "thread-project-assignments": {THREAD: {"projectKind":"local", "projectId":"legacy"}},
            "project-order":["legacy"],
        }
        self.write(self.initial)
        self.codex.joinpath(sync.GLOBAL + ".bak").write_bytes(self.live.read_bytes())
        with contextlib.closing(sqlite3.connect(self.codex / "state_5.sqlite")) as db:
            db.executescript("""
                CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, metadata TEXT);
                CREATE TABLE project_roots (project_id TEXT, position INTEGER, path TEXT);
                CREATE TABLE project_idempotency_keys (key TEXT, project_id TEXT);
                CREATE TABLE threads (id TEXT PRIMARY KEY, source TEXT, thread_source TEXT,
                    rollout_path TEXT, agent_role TEXT, agent_path TEXT, attached_thread_parent_id TEXT);
                INSERT INTO projects VALUES ('native', 'Here', '{}');
                INSERT INTO project_roots VALUES ('native', 0, '/fixture/source');
                INSERT INTO project_idempotency_keys VALUES ('legacy', 'native');
            """)
            for tid in (THREAD, OTHER_THREAD):
                (self.codex / "sessions").mkdir(exist_ok=True)
                (self.codex / "sessions" / (tid + ".jsonl")).touch()
                db.execute("INSERT INTO threads VALUES (?, 'vscode', 'user', ?, NULL, NULL, NULL)",
                           (tid, str(self.codex / "sessions" / (tid + ".jsonl"))))
            db.commit()
        self.store.prepare()
        self.store.enrollment.write_bytes(sync.packed(sync.make_policy(self.initial, 'all', True)))
        self.store.enrollment.chmod(0o600)

    def write(self, obj):
        self.live.write_text(json.dumps(obj, ensure_ascii=False))
        self.live.chmod(0o600)

    def read(self):
        return json.loads(self.live.read_bytes())

    def target(self, state):
        return state[sync.SCOPES]["inactive"][self.target_id]

    def check_applied(self):
        result = self.read()
        target = self.target(result)
        self.assertEqual(target["globals"]["local-projects"], self.initial["local-projects"])
        self.assertEqual(target["atoms"]["prompt-history"][THREAD], [{"plain": PROMPT}])
        self.assertNotIn("composer-prompt-drafts-v2", target["atoms"])
        self.assertEqual(result[sync.ATOMS], self.initial[sync.ATOMS])

    def test_newer_unknown_native_database_is_deferred(self):
        (self.codex/"state_6.sqlite").touch()
        before = self.live.read_bytes()
        with self.assertRaisesRegex(sync.Refusal, "unrecognized-native-database-version"):
            self.store.update("sync")
        self.assertEqual(before, self.live.read_bytes())
        self.assertFalse(self.store.pending.exists())

    def test_missing_baseline_after_success_does_not_reimport(self):
        self.store.update("sync")
        self.store.baseline.unlink()
        before = self.live.read_bytes()
        with self.assertRaisesRegex(sync.Refusal, "missing-baseline-needs-review"):
            self.store.update("sync")
        self.assertEqual(before, self.live.read_bytes())

    def test_enrollment_selects_project_grouping_once_and_keeps_sort(self):
        state = self.read()
        self.target(state)["atoms"][sync.VIEW_PREFS] = {"mode":"list", "chatSortMode":"manual"}
        self.write(state)
        self.store.update("sync")
        after = self.read()
        self.assertEqual(self.target(after)["atoms"][sync.VIEW_PREFS],
                         {"mode":"project", "chatSortMode":"manual"})
        self.target(after)["atoms"][sync.VIEW_PREFS]["mode"] = "connection"
        self.write(after)
        self.assertEqual(self.store.update("sync")["state"], "no-native-changes")

    def test_plan_apply_then_noop_has_no_file_churn(self):
        original = self.live.read_bytes()
        native_backup = self.codex / (sync.GLOBAL + ".bak")
        preview = self.store.update("plan")
        self.assertEqual(preview["state"], "preview")
        self.assertGreater(preview["counts"]["changed_fields"], 0)
        self.assertEqual(self.live.read_bytes(), original)
        self.assertFalse(self.store.baseline.exists())
        self.assertFalse(self.store.log.exists())
        applied = self.store.update("sync")
        self.check_applied()
        self.assertEqual(applied["state"], "synced")
        backups = list(self.store.backups.glob("*.json"))
        self.assertEqual(len(backups), 1)
        tracked = [self.live, self.store.baseline, self.store.log, native_backup, *backups]
        def signatures():
            return [(p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) for p in tracked]
        prior = signatures()
        self.assertEqual(self.store.update("sync")["state"], "no-native-changes")
        self.assertEqual(signatures(), prior)
        self.assertEqual(native_backup.read_bytes(), original)
        self.assertFalse(self.store.pending.exists())
        for obj in (preview, applied, json.loads(self.store.log.read_bytes())):
            log = json.dumps(obj)
            for private in (PROMPT, PRIVATE, THREAD, self.source_id, self.target_id, self.host):
                self.assertNotIn(private, log)

    def test_unproven_identity_refuses(self):
        state = self.read()
        self.target(state)["atoms"]["local-document-identity"]["userId"] = "changed"
        self.write(state)
        with self.assertRaisesRegex(sync.Refusal, "identity-proof-mismatch"):
            self.store.update("sync")
        self.assertEqual(self.read(), state)
        self.assertFalse(self.store.pending.exists())
        self.assertFalse(self.store.baseline.exists())

    def test_unmanaged_writer_after_preparation_is_preserved(self):
        real_replace = sync.replace
        expected = self.read()
        expected["new-native-setting"] = PRIVATE
        def concurrent_write(path, data):
            real_replace(path, data)
            if path == self.store.pending:
                self.write(expected)
        with mock.patch.object(sync, "replace", side_effect=concurrent_write):
            with self.assertRaisesRegex(sync.Refusal, "source-changed-deferred"):
                self.store.update("sync")
        self.assertEqual(self.read(), expected)
        self.assertFalse(self.store.pending.exists())
        self.assertFalse(self.store.baseline.exists())
        self.assertFalse(list(self.store.backups.iterdir()))

    def test_app_reopens_before_commit(self):
        before = self.live.read_bytes()
        self.store.is_offline = mock.Mock(side_effect=lambda: not self.store.pending.exists())
        with self.assertRaisesRegex(sync.Refusal, "deferred"):
            self.store.update("sync")
        self.assertEqual(self.live.read_bytes(), before)
        self.assertFalse(self.store.baseline.exists())
        self.assertFalse(self.store.pending.exists())
        self.assertFalse(list(self.store.backups.iterdir()))

    def test_restore_preserves_unrelated_settings(self):
        self.store.update("sync")
        edited = self.read()
        edited["unrelated-setting"] = PRIVATE
        self.target(edited)["atoms"]["composer-prompt-drafts-v2"] = {THREAD:PRIVATE}
        self.write(edited)
        self.assertEqual(self.store.update("restore")["state"], "restored")
        expected = self.initial
        expected["unrelated-setting"] = PRIVATE
        self.target(expected)["atoms"]["composer-prompt-drafts-v2"] = {THREAD:PRIVATE}
        self.assertEqual(self.read(), expected)

    def test_restore_preserves_sibling_scope_created_after_sync(self):
        self.store.update("sync")
        edited = self.read()
        self.target(edited)["atoms"]["prompt-history"][OTHER_THREAD] = [PRIVATE]
        self.write(edited)
        self.assertEqual(self.store.update("restore")["state"], "restored")
        self.assertEqual(self.target(self.read())["atoms"]["prompt-history"], {OTHER_THREAD:[PRIVATE]})

    def test_later_edit_to_restored_scope_is_never_overwritten(self):
        self.store.update("sync")
        edited = self.read()
        self.target(edited)["atoms"]["prompt-history"][THREAD].append(PRIVATE)
        self.write(edited)
        before = self.live.read_bytes()
        with self.assertRaisesRegex(sync.Refusal, "restore-point-diverged"):
            self.store.update("restore")
        self.assertEqual(self.live.read_bytes(), before)
        self.assertFalse(self.store.pending.exists())

    def test_owner_moves_between_active_and_inactive_before_recovery(self):
        with mock.patch.object(self.store, "finish", side_effect=PowerLoss()):
            with self.assertRaises(PowerLoss):
                self.store.update("sync")
        after = self.read()
        active_globals = {k:v for k,v in after.items() if k not in (sync.ATOMS, sync.SCOPES)}
        new_active = self.target(after)
        switched = dict(new_active["globals"])
        switched[sync.ATOMS] = new_active["atoms"]
        switched[sync.SCOPES] = {"owner":self.target_id, "inactive":{
            self.source_id: {"globals":active_globals, "atoms":after[sync.ATOMS]},
        }}
        self.write(switched)
        result = self.store.update("sync")
        self.assertEqual(result["state"], "no-native-changes")
        self.assertEqual(self.read(), switched)
        self.assertFalse(self.store.pending.exists())
        self.assertEqual(len(list(self.store.backups.glob("*.json"))), 1)

    def test_divergent_partial_transaction_requires_review(self):
        with mock.patch.object(self.store, "finish", side_effect=PowerLoss()):
            with self.assertRaises(PowerLoss):
                self.store.update("sync")
        changed = self.read()
        self.target(changed)["atoms"]["prompt-history"][THREAD] = [PRIVATE]
        self.write(changed)
        pending = self.store.pending.read_bytes()
        with self.assertRaisesRegex(sync.Refusal, "interrupted-transaction-needs-review"):
            self.store.update("sync")
        self.assertEqual(self.read(), changed)
        self.assertEqual(self.store.pending.read_bytes(), pending)
        self.assertFalse(self.store.baseline.exists())

    def test_power_loss_before_rename_replans_after_divergence(self):
        real_rename = os.replace
        def crash_at_rename(src, dest):
            if dest == self.live:
                raise PowerLoss()
            real_rename(src, dest)
        with mock.patch.object(sync.os, "replace", side_effect=crash_at_rename):
            with self.assertRaises(PowerLoss):
                self.store.update("sync")
        self.assertTrue(self.store.pending.exists())
        self.assertFalse(self.store.baseline.exists())
        self.assertEqual(self.read(), self.initial)
        state = self.read()
        state["unrelated-setting"] = PRIVATE
        self.write(state)
        self.store.update("sync")
        self.check_applied()
        self.assertEqual(self.read()["unrelated-setting"], PRIVATE)
        self.assertFalse(self.store.pending.exists())
        self.assertEqual(len(list(self.store.backups.glob("*.json"))), 1)

    def test_power_loss_after_rename_keeps_applied_result_and_later_edits(self):
        with mock.patch.object(self.store, "finish", side_effect=PowerLoss()):
            with self.assertRaises(PowerLoss):
                self.store.update("sync")
        self.check_applied()
        self.assertTrue(self.store.pending.exists())
        self.assertFalse(self.store.baseline.exists())
        state = self.read()
        state["unrelated-setting"] = PRIVATE
        self.write(state)
        self.assertEqual(self.store.update("sync")["state"], "no-native-changes")
        self.assertEqual(self.read(), state)
        self.assertTrue(self.store.baseline.exists())
        self.assertFalse(self.store.pending.exists())
        self.assertEqual(len(list(self.store.backups.glob("*.json"))), 1)

    def test_cli_error_logs_only_code(self):
        state = self.read()
        self.target(state)["atoms"]["local-document-identity"]["userId"] = PRIVATE
        self.write(state)
        out, err = io.StringIO(), io.StringIO()
        old_mask = os.umask(0o077)
        self.addCleanup(os.umask, old_mask)
        with mock.patch.object(sync, "Store", return_value=self.store), mock.patch.object(sys, "argv", ["sync.py","sync"]):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
                sync.main()
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue().strip(), "identity-proof-mismatch")
        saved = json.loads(self.store.log.read_bytes())
        self.assertEqual(set(saved), {"time","state"})
        self.assertEqual(saved["state"], "identity-proof-mismatch")


if __name__ == "__main__":
    unittest.main()
