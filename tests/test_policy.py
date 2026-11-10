"""Enrollment, read-only preview, and policy-bound transactions on synthetic state."""
import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import sys
import unittest
from unittest import mock

import manage
import sync
import test_sync as fixture


class PolicyTests(unittest.TestCase):
    def setUp(self):
        f = fixture.StoreTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.f = f
        self.work = f.root.parent
        self.store = sync.Store(self.work/'uninstalled', f.codex, lambda: True)

    def cli(self, *args):
        output = io.StringIO()
        with mock.patch.object(sys, 'argv', ['manage.py', *args]), \
                mock.patch.object(manage.sync, 'Store', return_value=self.store), \
                mock.patch.dict(os.environ, {'CODEX_HOME': ''}), contextlib.redirect_stdout(output):
            manage.main()
        return json.loads(output.getvalue())

    def test_portable_profiles_and_preview_do_not_install_or_modify(self):
        def signatures():
            return sorted((p.name, p.stat().st_mtime_ns, p.read_bytes()) for p in self.f.codex.rglob('*')
                          if p.is_file())
        before = signatures()
        profiles = self.cli('profiles')
        self.assertEqual(profiles, sorted([
            {'label': self.f.source_id[:12], 'active': True},
            {'label': self.f.target_id[:12], 'active': False}], key=lambda p:p['label']))
        for p in profiles:
            self.assertRegex(p['label'], r'^[0-9a-f]{12}$')
        report = self.cli('preview','--profiles','existing')
        self.assertEqual(report['state'], 'preview')
        self.assertGreater(report['counts']['changed_fields'], 0)
        self.assertEqual(report['counts']['history'], {'enabled':False})
        self.assertEqual(before, signatures())
        self.assertFalse(self.store.data.parent.exists())

    def test_install_needs_profiles_and_a_recognized_bundle_before_any_writes(self):
        with mock.patch.object(sys, 'argv', ['manage.py','install']), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            manage.main()
        with mock.patch.object(manage, 'recognized_app', side_effect=RuntimeError('no-bundle')), \
             mock.patch.object(manage, 'ROOT', self.store.data.parent), self.assertRaisesRegex(RuntimeError, 'no-bundle'):
            self.cli('install','--profiles','all')
        self.assertFalse(self.store.data.parent.exists())
        apps = self.work/'Apps'
        info = apps/'Codex.app/Contents'
        (info/'MacOS').mkdir(parents=True)
        (info/'MacOS/Codex').touch(mode=0o700)
        (info/'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':'com.openai.codex',
                                                       'CFBundleExecutable':'Codex'}))
        manage.recognized_app([apps])

    def test_refuses_custom_home(self):
        with mock.patch.dict(os.environ, {'CODEX_HOME': str(self.work/'elsewhere')}), \
             mock.patch.object(sys, 'argv', ['manage.py','profiles']), \
             self.assertRaisesRegex(sync.Refusal, 'default-CODEX_HOME'):
            manage.main()

    def test_selected_source_cannot_export_to_unenrolled_or_future_profiles(self):
        f = self.f
        state = f.read()
        frozen = sync.make_policy(state, 'current')
        single = sync.make_policy(state, f.source_id[:12])
        star = sync.make_policy(state, 'all')
        new = fixture.owner('new','workspace-c')
        state[sync.SCOPES]['inactive'][new] = {
            'globals': {}, 'atoms': {'local-document-identity': {'userId':'new','accountId':'workspace-c'}}}
        inv = sync.local_inventory(f.codex)
        for pol, count in [(single,1),(frozen,2),(star,3)]:
            candidate = json.loads(json.dumps(state))
            base, _, counts = sync.compute(candidate, {'version':1}, inv, pol)
            self.assertEqual(counts['profiles'], count)
            self.assertEqual('local-projects' in candidate[sync.SCOPES]['inactive'][new]['globals'], pol == star)
            self.assertNotIn('history', base)
            self.assertNotIn(fixture.PROMPT, json.dumps(base))
            if pol == single:
                self.assertEqual(f.target(candidate), f.target(state))

    def test_legacy_policy_invalid_labels_and_changes_fail_closed(self):
        f = self.f
        with self.assertRaisesRegex(sync.Refusal, 'unknown-profile-label'):
            sync.make_policy(f.initial, 'deadbeef1234')
        pol = sync.make_policy(f.initial, 'all')
        with self.assertRaisesRegex(sync.Refusal, 'legacy-state-requires'):
            sync.guard_scope({'projects':{}}, pol)
        sync.guard_scope({'projects':{}}, sync.make_policy(f.initial, 'all', True))
        f.store.update('sync')
        f.store.enrollment.write_bytes(sync.packed(pol))
        with self.assertRaisesRegex(sync.Refusal, 'enrollment-policy-mismatch'):
            f.store.update('sync')
        f.store.enrollment.unlink()
        with self.assertRaisesRegex(sync.Refusal, 'missing-or-unsupported-enrollment-policy'):
            f.store.update('sync')

    def test_reinstall_scope_mismatch_does_not_replace_worker_or_policy(self):
        f = self.f
        original = f.store.enrollment.read_bytes()
        with mock.patch.object(manage, 'PLIST', self.work/'LaunchAgents/fixture.plist'), \
                mock.patch.object(manage, 'BIN', self.work/'absentbin'), \
                mock.patch.object(manage, 'check_service'), mock.patch.object(sync, 'Store', return_value=f.store), \
                mock.patch.object(manage, 'stop') as stop, \
                self.assertRaisesRegex(RuntimeError, 'Enrollment differs'):
            manage.install(sync.make_policy(f.initial, 'existing'))
        stop.assert_not_called()
        self.assertEqual(original, f.store.enrollment.read_bytes())

    def test_pending_native_project_gains_cloud_metadata(self):
        f = self.f
        original = f.live.read_bytes()
        old_replace = sync.replace
        def add_metadata(path, data):
            old_replace(path, data)
            if path == f.store.pending:
                with contextlib.closing(sqlite3.connect(f.codex/'state_5.sqlite')) as db:
                    db.execute("UPDATE projects SET metadata=? WHERE id=?", ('{"cloud":"test"}','native'))
                    db.commit()
        with mock.patch.object(sync, 'replace', side_effect=add_metadata), \
                self.assertRaisesRegex(sync.Refusal, 'native-inventory-changed-deferred'):
            f.store.update('sync')
        self.assertEqual(f.live.read_bytes(), original)
        self.assertFalse(f.store.pending.exists())
        self.assertFalse(f.store.baseline.exists())


if __name__ == '__main__':
    unittest.main()
