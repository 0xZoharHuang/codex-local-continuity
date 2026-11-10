"""Exercise the public command surface without launching an app or using a real identity."""
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import capabilities
import manage
import sync
import test_sync as fixtures


class ControlsTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.StoreTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.root = self.f.root
        self.bin = self.root/'bin'
        self.bin.mkdir(mode=0o700)
        self.manifest = {'sync.py': manage.digest(Path(manage.__file__).resolve().with_name('sync.py'))}

    def invoke(self, *args):
        out = io.StringIO()
        with patch.object(manage, 'ROOT', self.root), patch.object(manage, 'DATA', self.f.store.data), \
             patch.object(manage, 'BIN', self.bin), patch.object(sync, 'Store', return_value=self.f.store), \
             patch.object(manage, 'check_bin', return_value=self.manifest), \
             patch.object(sys,'argv',['manage.py',*args]), contextlib.redirect_stdout(out):
            manage.main()
        return json.loads(out.getvalue())

    def test_change_features_and_preview_without_install_or_desktop_write(self):
        original = self.f.live.read_bytes()
        choice = self.invoke('configure', '--features', 'history')
        self.assertEqual(choice['state'],'configured')
        self.assertEqual(self.invoke('settings','--json')['features'],'history')
        rows = {row['id']:row for row in self.invoke('capabilities','--json')}
        self.assertEqual(rows['recall']['setting'],'enabled')
        self.assertEqual(rows['folders']['setting'],'disabled')
        self.assertEqual(rows['plugins']['control'],'local-audit')
        self.assertEqual(rows['models']['control'],'account')
        self.assertEqual(self.invoke('preview','--features','off')['counts']['changed_fields'],0)
        self.assertEqual(self.f.live.read_bytes(),original)

    def test_editor_stale_choice_cannot_override_later_policy_even_after_roundtrip(self):
        token = manage.digest(self.f.store.enrollment)
        original = self.f.store.policy()
        self.f.store.reconfigure(self.f.store.propose(features='off'))
        self.f.store.reconfigure(original)
        with patch.object(manage,'ROOT',self.root), patch.object(manage,'DATA',self.f.store.data), \
             patch.object(manage,'check_bin',return_value=self.manifest), \
             self.assertRaisesRegex(RuntimeError,'policy changed'):
            manage.apply_settings(self.f.store,token,self.f.store.propose(features='projects'))

    def test_capabilities_reflect_each_switch_independently(self):
        for mode, expected in {
            'folders':{'folders'},'folders-history':{'folders','recall'},'projects':{'folders','projects'},
            'both':{'folders','projects','recall'},'history':{'recall'},'off':set(),
        }.items():
            actual={r['id'] for r in capabilities.report(mode) if r['setting']=='enabled'}
            self.assertEqual(actual,expected)
