"""Real transaction semantics on a synthetic migrated desktop, never the running App."""
import copy
import json
import os
import unittest
from unittest import mock

import sync
import modern_sync
import native_layout
import test_sync as fixture


class ModernSyncTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.StoreTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.store=self.f.store
        self.source=self.f.source_id
        self.target=self.f.target_id
        self.legacy=copy.deepcopy(self.f.initial)
        a=self.legacy[sync.ATOMS]
        b=self.legacy[sync.SCOPES]['inactive'][self.target]
        self.modern={k:copy.deepcopy(v) for k,v in self.legacy.items() if k not in (sync.SCOPES,sync.ATOMS)}
        self.modern.update({
            'electron-renderer-content-owner':self.source,
            'electron-owner-atoms:'+self.source:{'local-document-identity':a['local-document-identity']},
            'electron-owner-atoms:'+self.target:copy.deepcopy(b['atoms']),
            sync.ATOMS:{'prompt-history':copy.deepcopy(a['prompt-history']),
                        'composer-prompt-drafts-v2':{fixture.THREAD:fixture.PRIVATE}},
        })
        self.f.write(self.modern)

    def switch(self, owner):
        s=self.f.read()
        s['electron-renderer-content-owner']=owner
        s[sync.ATOMS]={'prompt-history':{},'composer-prompt-drafts-v2':{}}
        self.f.write(s)

    def test_capture_then_native_clear_then_exit_restore(self):
        original=self.f.live.read_bytes()
        with mock.patch.object(self.store,'is_offline',side_effect=AssertionError('live app check')):
            result=self.store.update('capture')
        self.assertEqual(result['state'],'captured')
        self.assertEqual(self.f.live.read_bytes(),original)
        self.switch(self.target)
        self.store.update('capture')
        before=self.f.read()
        self.assertEqual(self.store.update('sync')['state'],'synced-modern-history')
        after=self.f.read()
        self.assertEqual(after[sync.ATOMS]['prompt-history'][fixture.THREAD],[{'plain':fixture.PROMPT}])
        x=copy.deepcopy(after);x[sync.ATOMS]['prompt-history']=before[sync.ATOMS]['prompt-history']
        self.assertEqual(x,before)
        self.assertEqual(self.store.update('sync')['state'],'modern-history-current')
        self.assertEqual(self.store.update('restore')['state'],'restored-modern-history')
        self.assertEqual(self.f.read(),before)

    def test_new_native_projects_are_inert_and_no_project_db_needed(self):
        self.store.enrollment.write_bytes(sync.packed(sync.make_policy(self.f.read(),'all')))
        raw=self.f.live.read_bytes()
        with mock.patch.object(sync,'local_inventory',side_effect=AssertionError('DB query')):
            result=self.store.update('sync')
        self.assertEqual(result['state'],'native-project-sharing')
        self.assertEqual(raw,self.f.live.read_bytes())

    def test_unknown_layout_and_old_rollback_cannot_write(self):
        unknown={'new-version':True}
        self.f.write(unknown)
        raw=self.f.live.read_bytes()
        with self.assertRaisesRegex(sync.Refusal,'unsupported-identity-layout'):
            self.store.update('sync')
        self.assertEqual(raw,self.f.live.read_bytes())
        self.f.write(self.legacy)
        self.store.update('sync')
        self.f.write(self.modern)
        raw=self.f.live.read_bytes()
        with self.assertRaisesRegex(sync.Refusal,'incompatible-recovery-layout'):
            self.store.update('restore')
        self.assertEqual(raw,self.f.live.read_bytes())

    def test_capture_declines_moving_snapshot_and_unenrolled_owner(self):
        raw=self.f.live.read_bytes()
        real=sync.read_regular
        calls=[]
        def moving(path,*a):
            data=real(path,*a)
            if path==self.f.live:
                calls.append(1)
                if len(calls)>2:return data+b' '
            return data
        with mock.patch.object(sync,'read_regular',side_effect=moving):
            with self.assertRaisesRegex(sync.Refusal,'capture-source-changed'):
                self.store.update('capture')
        self.assertEqual(raw,self.f.live.read_bytes())
        self.assertFalse(modern_sync.paths(self.store)[0].exists())
        self.store.enrollment.write_bytes(sync.packed(sync.make_policy(self.f.read(),self.target[:12],True)))
        self.assertEqual(self.store.update('capture')['state'],'capture-no-enrolled-active-profile')

    def test_reopen_and_interrupted_commit_recover(self):
        self.store.update('capture')
        self.switch(self.target)
        raw=self.f.live.read_bytes()
        with mock.patch.object(self.store,'is_offline',side_effect=lambda: not self.store.pending.exists()):
            with self.assertRaisesRegex(sync.Refusal,'deferred'):
                self.store.update('sync')
        self.assertEqual(raw,self.f.live.read_bytes())
        with mock.patch.object(modern_sync,'finish',side_effect=fixture.PowerLoss()):
            with self.assertRaises(fixture.PowerLoss):
                self.store.update('sync')
        self.assertTrue(self.store.pending.exists())
        self.store.update('sync')
        self.assertFalse(self.store.pending.exists())
        self.assertEqual(self.f.read()[sync.ATOMS]['prompt-history'][fixture.THREAD],[{'plain':fixture.PROMPT}])

    def test_seed_from_old_receipts_preserves_native_and_no_past_epoch(self):
        self.f.write(self.legacy)
        # Make the target have an existing input so there is a recoverable preimage.
        s=self.f.read()
        s[sync.SCOPES]['inactive'][self.target]['atoms']['prompt-history']={fixture.THREAD:[{'plain':'Earlier local query'}]}
        self.f.write(s)
        self.store.update('sync')
        self.f.write(self.modern)
        raw=self.f.live.read_bytes()
        self.store.update('capture')
        self.assertEqual(self.f.live.read_bytes(),raw)
        self.switch(self.target)
        self.store.update('sync')
        text=[x.get('plain') if isinstance(x,dict) else x for x in self.f.read()[sync.ATOMS]['prompt-history'][fixture.THREAD]]
        self.assertIn('Earlier local query',text)
        self.assertIn(fixture.PROMPT,text)

    def test_doctor_verifies_new_identity(self):
        import doctor
        report,_,pair,_=doctor.identity(self.f.codex,b'x'*32)
        self.assertEqual(report,{'state':'verified','native_layout':'shared-v2'})
        self.assertEqual(pair,('source','workspace-a'))

    def test_pending_owner_switch_declines_and_preserves_journal_and_native(self):
        self.store.update('capture')
        self.switch(self.target)
        with mock.patch.object(modern_sync,'finish',side_effect=fixture.PowerLoss()):
            with self.assertRaises(fixture.PowerLoss):
                self.store.update('sync')
        journal=self.store.pending.read_bytes()
        self.switch(self.source)
        native=self.f.live.read_bytes()
        cache=modern_sync.paths(self.store)[0].read_bytes()
        with self.assertRaisesRegex(sync.Refusal,'pending-owner-changed-needs-review'):
            self.store.update('sync')
        self.assertEqual(self.store.pending.read_bytes(),journal)
        self.assertEqual(self.f.live.read_bytes(),native)
        self.assertEqual(modern_sync.paths(self.store)[0].read_bytes(),cache)

    def test_stale_capture_policy_and_native_inventory_refuse_before_commit(self):
        real_policy=self.store.policy()
        different=sync.make_policy(self.f.read(),self.source[:12],True)
        with mock.patch.object(self.store,'policy',side_effect=[real_policy,real_policy,different]):
            with self.assertRaisesRegex(sync.Refusal,'capture-source-changed'):
                self.store.update('capture')
        self.assertFalse(modern_sync.paths(self.store)[0].exists())
        self.store.update('capture')
        self.switch(self.target)
        native=self.f.live.read_bytes()
        real=sync.local_inventory
        calls=[]
        def changing(*args,**kwargs):
            inv=real(*args,**kwargs)
            calls.append(1)
            if len(calls)>1:
                inv['history_threads']=set()
            return inv
        with mock.patch.object(sync,'local_inventory',side_effect=changing):
            with self.assertRaisesRegex(sync.Refusal,'source-changed-deferred'):
                self.store.update('sync')
        self.assertEqual(self.f.live.read_bytes(),native)
        self.assertFalse(self.store.pending.exists())

    def test_cache_private_only_safe_text_and_unrelated_native_fields_kept(self):
        s=self.f.read()
        opaque={'plain':'opaque-local-body','document':{'type':'future-reference','private':'opaque-reference'}}
        s[sync.ATOMS]['prompt-history'][fixture.THREAD].append(opaque)
        s[sync.ATOMS]['prompt-history']['global']=['excluded-global-body']
        s[sync.ATOMS]['prompt-history']['cloud-id']=['excluded-cloud-body']
        self.f.write(s)
        native=self.f.live.read_bytes()
        original_backup=self.f.codex.joinpath(sync.GLOBAL+'.bak').read_bytes()
        self.store.update('capture')
        file,status=modern_sync.paths(self.store)
        cache=file.read_text()
        for secret in ('opaque-local-body','opaque-reference','excluded-global-body','excluded-cloud-body',fixture.PRIVATE):
            self.assertNotIn(secret,cache)
        self.assertEqual(file.stat().st_mode&0o777,0o600)
        self.assertEqual(status.stat().st_mode&0o777,0o600)
        self.assertEqual(self.f.live.read_bytes(),native)
        self.switch(self.target)
        self.store.update('sync')
        for backup in self.store.backups.glob('*.json'):
            self.assertEqual(backup.stat().st_mode&0o777,0o600)
        self.assertEqual(self.f.codex.joinpath(sync.GLOBAL+'.bak').read_bytes(),original_backup)

    def test_fresh_selected_preview_excludes_cache_and_old_receipt_owner(self):
        self.store.update('capture')
        self.switch(self.target)
        native=self.f.live.read_bytes()
        cache=modern_sync.paths(self.store)[0].read_bytes()
        chosen=sync.make_policy(self.f.read(),self.target[:12],True)
        result=self.store.update('plan',chosen)
        self.assertEqual(result['counts']['changed_fields'],0)
        self.assertEqual(native,self.f.live.read_bytes())
        self.assertEqual(cache,modern_sync.paths(self.store)[0].read_bytes())
        self.store.reconfigure(chosen)
        self.store.update('sync')
        self.assertEqual(self.f.read()[sync.ATOMS]['prompt-history'],{})

    def test_cross_generation_modern_recovery_and_legacy_pending_rejected(self):
        self.store.update('capture')
        self.switch(self.target)
        self.store.update('sync')
        point=next(self.store.backups.glob('*.json'))
        old=point.read_bytes()
        self.store.reconfigure(self.store.propose(features='history'))
        newpoint=self.store.backups/point.name
        sync.replace(newpoint,old)
        raw=self.f.live.read_bytes()
        with self.assertRaisesRegex(sync.Refusal,'incompatible-recovery-layout'):
            self.store.update('restore')
        self.assertEqual(raw,self.f.live.read_bytes())
        newpoint.unlink()
        sync.replace(self.store.pending,sync.packed({'changes':[],'before':'unknown'}))
        with self.assertRaisesRegex(sync.Refusal,'incompatible-recovery-layout'):
            self.store.update('sync')
        self.assertEqual(raw,self.f.live.read_bytes())

    def test_restore_preserves_excluded_scopes_and_new_siblings(self):
        self.store.update('capture')
        self.switch(self.target)
        before=self.f.read()
        before[sync.ATOMS]['prompt-history']['global']=['global-before']
        before[sync.ATOMS]['prompt-history']['cloud-scope']=['cloud-before']
        self.f.write(before)
        self.store.update('sync')
        receipt=sync.read_json(next(self.store.backups.glob('*.json')).read_bytes())
        self.assertEqual(receipt['scope_keys'],[fixture.THREAD])
        self.assertNotIn('global-before',json.dumps(receipt))
        self.assertNotIn('cloud-before',json.dumps(receipt))
        after=self.f.read()
        after[sync.ATOMS]['prompt-history']['global']=['global-new']
        after[sync.ATOMS]['prompt-history'][fixture.OTHER_THREAD]=['new-sibling']
        self.f.write(after)
        self.store.update('restore')
        restored=self.f.read()[sync.ATOMS]['prompt-history']
        self.assertEqual(restored,{'global':['global-new'],'cloud-scope':['cloud-before'],
                                   fixture.OTHER_THREAD:['new-sibling']})

    def test_restore_removes_atom_created_by_sync_when_it_becomes_empty(self):
        self.store.update('capture')
        self.switch(self.target)
        state=self.f.read()
        del state[sync.ATOMS]['prompt-history']
        self.f.write(state)
        self.store.update('sync')
        receipt=sync.read_json(next(self.store.backups.glob('*.json')).read_bytes())
        self.assertTrue(receipt['created_atom'])
        self.store.update('restore')
        self.assertNotIn('prompt-history',self.f.read()[sync.ATOMS])
