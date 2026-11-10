import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sync
import test_sync as fixtures


class StorageChecks(unittest.TestCase):
    def test_stale_or_redirected_session_excludes_recall(self):
        fixture = fixtures.StoreTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        db_root = fixture.codex
        path = db_root/'sessions'/(fixtures.THREAD+'.jsonl')
        path.unlink()
        for redirected in (False, True):
            if redirected:
                path.symlink_to(db_root/sync.GLOBAL)
            inv = sync.local_inventory(db_root)
            self.assertIn(fixtures.THREAD, inv['threads'])
            self.assertNotIn(fixtures.THREAD, inv['history_threads'])

    def test_custom_protection_prevents_replace(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'global.json'
            p.write_text('{}')
            p.chmod(0o400)
            with self.assertRaisesRegex(sync.Refusal, 'protection'):
                sync.check_native_metadata(p)

    @unittest.skipUnless(os.uname().sysname == 'Darwin', 'Darwin ACL/attribute checks')
    def test_external_attribute_and_acl_are_preserved_by_refusing(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'global.json'
            p.write_text('{}')
            for listing, code in ((['com.apple.FinderInfo', ''], 'attributes'),
                                  (['', '-rw------- 1 user staff state\n 0: user:example allow read'], 'acl')):
                answers = [mock.Mock(stdout=s) for s in listing]
                with mock.patch.object(sync.subprocess, 'run', side_effect=answers):
                    with self.assertRaisesRegex(sync.Refusal, code):
                        sync.check_native_metadata(p)
