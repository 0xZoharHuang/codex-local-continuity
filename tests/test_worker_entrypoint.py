"""Exercise executable worker identity, not just imported functions."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import test_modern_sync as modern_fixture


class WorkerEntryPoint(unittest.TestCase):
    def test_real_script_preserves_semantic_refusal_and_transient_status(self):
        f = modern_fixture.ModernSyncTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.addCleanup(f.f.doCleanups)
        with tempfile.TemporaryDirectory() as temporary:
            home=Path(temporary).resolve()
            native=home/'.codex'
            native.mkdir(mode=0o700)
            shutil.copyfile(f.f.live,native/f.f.live.name)
            root=home/'Library/Application Support/CodexLocalContinuity'
            data=root/'data'
            data.mkdir(parents=True,mode=0o700)
            root.chmod(0o700)
            (data/'backups').mkdir(mode=0o700)
            (data/'policy.json').write_bytes(f.store.enrollment.read_bytes())
            (data/'policy.json').chmod(0o600)
            path=Path(__file__).resolve().parents[1]/'src/sync.py'
            code=r"""
import runpy,sys,types
from pathlib import Path
from unittest.mock import patch
source,home,reason=sys.argv[1:]
fake=types.ModuleType("modern_sync")
def capture(store):
    import sync
    raise sync.Refusal(reason)
fake.capture=capture
sys.modules["modern_sync"]=fake
sys.argv=[source,"capture"]
with patch.object(Path,"home",return_value=Path(home)):
    runpy.run_path(source,run_name="__main__")
"""
            def execute(reason):
                return subprocess.run([sys.executable,'-I','-B','-c',code,str(path),str(home),reason],
                                      text=True,capture_output=True,check=False)
            original=(native/f.f.live.name).read_bytes()
            transient=execute('capture-source-changed')
            self.assertEqual(transient.returncode,2)
            self.assertEqual(transient.stderr.strip(),'capture-source-changed')
            self.assertFalse((data/'capture-status.json').exists())
            semantic=execute('recall-scope-mismatch')
            self.assertEqual(semantic.returncode,2)
            self.assertEqual(json.loads((data/'capture-status.json').read_bytes())['state'],'recall-scope-mismatch')
            self.assertEqual(original,(native/f.f.live.name).read_bytes())
