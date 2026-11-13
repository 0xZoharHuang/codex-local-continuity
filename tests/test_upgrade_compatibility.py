"""Upgrade/epoch races on disposable native metadata only."""
import copy
import json
import sqlite3
import unittest
from unittest import mock

import modern_sync
import native_layout
import recall
import sync
import test_modern_sync as fixture
import test_sync


class UpgradeCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ModernSyncTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.store = self.f.store
        self.native = self.f.f

    def rotate_to_equal_policy(self):
        original = self.store.policy()
        alternate = self.store.propose(features='history')
        self.store.reconfigure(alternate)
        self.store.reconfigure(original)

    def test_same_policy_reconfigure_is_exact_noop(self):
        self.store.update('capture')
        before = self.store.enrollment.read_bytes()
        cache, _ = modern_sync.paths(self.store)
        snapshot = (cache.read_bytes(), cache.stat().st_mtime_ns)
        self.assertEqual(self.store.reconfigure(copy.deepcopy(self.store.policy()))['state'],
                         'configuration-unchanged')
        self.assertEqual(self.store.enrollment.read_bytes(), before)
        self.assertEqual(snapshot, (cache.read_bytes(), cache.stat().st_mtime_ns))

    def test_capture_same_policy_new_epoch_is_not_committed(self):
        original = recall.capture
        def switched(*args, **kwargs):
            result = original(*args, **kwargs)
            self.rotate_to_equal_policy()
            return result
        native = self.native.live.read_bytes()
        with mock.patch.object(recall, 'capture', side_effect=switched):
            with self.assertRaisesRegex(sync.Refusal, 'changed|mismatch|deferred'):
                self.store.update('capture')
        self.assertEqual(self.native.live.read_bytes(), native)
        self.assertFalse(modern_sync.paths(self.store)[0].exists())

    def test_noop_merge_same_policy_new_epoch_is_not_committed(self):
        original = recall.merge
        def switched(*args, **kwargs):
            result = original(*args, **kwargs)
            self.rotate_to_equal_policy()
            return result
        native = self.native.live.read_bytes()
        with mock.patch.object(recall, 'merge', side_effect=switched):
            with self.assertRaisesRegex(sync.Refusal, 'changed|mismatch|deferred'):
                self.store.update('sync')
        self.assertEqual(self.native.live.read_bytes(), native)
        self.assertFalse(modern_sync.paths(self.store)[0].exists())

    def test_noop_merge_narrowed_group_is_not_committed(self):
        original = recall.merge
        def switched(*args, **kwargs):
            result = original(*args, **kwargs)
            self.store.reconfigure(self.store.propose(self.f.target[:12], 'history'))
            return result
        native = self.native.live.read_bytes()
        with mock.patch.object(recall, 'merge', side_effect=switched):
            with self.assertRaisesRegex(sync.Refusal, 'changed|mismatch|deferred'):
                self.store.update('sync')
        self.assertEqual(self.native.live.read_bytes(), native)
        self.assertFalse(modern_sync.paths(self.store)[0].exists())

    def test_capture_changed_policy_file_bytes_is_not_committed(self):
        original = recall.capture
        def rewritten(*args, **kwargs):
            result = original(*args, **kwargs)
            self.store.enrollment.write_bytes(self.store.enrollment.read_bytes() + b'\n')
            return result
        native = self.native.live.read_bytes()
        with mock.patch.object(recall, 'capture', side_effect=rewritten):
            with self.assertRaisesRegex(sync.Refusal, 'changed|mismatch|deferred'):
                self.store.update('capture')
        self.assertEqual(self.native.live.read_bytes(), native)
        self.assertFalse(modern_sync.paths(self.store)[0].exists())

    def test_forward_compatible_unrelated_fields_stay_identical(self):
        self.store.update('capture')
        self.f.switch(self.f.target)
        state = self.native.read()
        state['future-ui-setting'] = {'opaque': ['keep exactly']}
        state[sync.ATOMS]['future-widget'] = {'colors': 'new opaque metadata'}
        self.native.write(state)
        self.store.update('sync')
        after = self.native.read()
        self.assertEqual(after['future-ui-setting'], state['future-ui-setting'])
        self.assertEqual(after[sync.ATOMS]['future-widget'], state[sync.ATOMS]['future-widget'])
        self.assertEqual(after['local-projects'], state['local-projects'])

    def test_ambiguous_v3_ownership_markers_refuse(self):
        state = self.native.read()
        state['electron-account-renderer-state-v3'] = {'owner': self.f.source, 'mode': 'future'}
        self.native.write(state)
        native = self.native.live.read_bytes()
        with self.assertRaisesRegex(sync.Refusal, 'layout|ownership-version'):
            self.store.update('sync')
        self.assertEqual(native, self.native.live.read_bytes())

    def test_foreign_identity_in_named_native_owner_refuse(self):
        state = self.native.read()
        state[native_layout.OWNER_ATOMS + self.f.source][native_layout.IDENTITY]['userId'] = 'different'
        self.native.write(state)
        native = self.native.live.read_bytes()
        with self.assertRaisesRegex(sync.Refusal, 'identity-proof-mismatch'):
            self.store.update('capture')
        self.assertEqual(native, self.native.live.read_bytes())
        self.assertFalse(modern_sync.paths(self.store)[0].exists())

    def test_modern_pending_after_legacy_downgrade_retained_for_review(self):
        self.store.update('capture')
        self.f.switch(self.f.target)
        with mock.patch.object(modern_sync, 'finish', side_effect=test_sync.PowerLoss()):
            with self.assertRaises(test_sync.PowerLoss):
                self.store.update('sync')
        pending = self.store.pending.read_bytes()
        self.native.write(self.f.legacy)
        native = self.native.live.read_bytes()
        with self.assertRaises((sync.Refusal, KeyError)):
            self.store.update('sync')
        self.assertEqual(native, self.native.live.read_bytes())
        self.assertEqual(pending, self.store.pending.read_bytes())

    def test_policy_change_before_dirty_commit_preserves_native_and_journal(self):
        self.store.update('capture')
        self.f.switch(self.f.target)
        original = sync.replace
        policy = self.store.policy()
        changed = self.store.propose(self.f.target[:12], 'history')
        native = self.native.live.read_bytes()
        def replace(path, raw):
            original(path, raw)
            if path == self.store.pending:
                # Simulate an unmanaged writer bypassing administrative job locks.
                self.store.enrollment.write_bytes(sync.packed(changed))
        with mock.patch.object(sync, 'replace', side_effect=replace):
            with self.assertRaisesRegex(sync.Refusal, 'changed|mismatch|deferred'):
                self.store.update('sync')
        self.assertEqual(native, self.native.live.read_bytes())
        self.assertFalse(self.store.pending.exists())
        self.store.enrollment.write_bytes(sync.packed(policy))

    def test_restore_revalidates_thread_is_still_eligible(self):
        self.store.update('capture')
        self.f.switch(self.f.target)
        state = self.native.read()
        state[sync.ATOMS]['prompt-history'][test_sync.THREAD] = ['target-original']
        self.native.write(state)
        self.store.update('sync')
        with sqlite3.connect(self.native.codex/'state_5.sqlite') as db:
            db.execute('UPDATE threads SET source=? WHERE id=?', ('cloud', test_sync.THREAD))
        native = self.native.live.read_bytes()
        with self.assertRaisesRegex(sync.Refusal, 'eligib|scope|inventory|no-longer-local'):
            self.store.update('restore')
        self.assertEqual(native, self.native.live.read_bytes())

    def test_fast_relaunch_during_last_native_read_defers(self):
        self.store.update('capture')
        self.f.switch(self.f.target)
        native = self.native.live.read_bytes()
        launched = []
        real_read = sync.read_regular
        def read(path, *args, **kwargs):
            raw = real_read(path, *args, **kwargs)
            if path == self.native.live and self.store.pending.exists():
                launched.append(True)
            return raw
        with mock.patch.object(sync, 'read_regular', side_effect=read), \
                mock.patch.object(self.store, 'is_offline', side_effect=lambda: not launched):
            with self.assertRaisesRegex(sync.Refusal, 'changed|deferred'):
                self.store.update('sync')
        self.assertEqual(native, self.native.live.read_bytes())
        self.assertFalse(self.store.pending.exists())
