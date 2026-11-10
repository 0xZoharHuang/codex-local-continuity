"""Private generation, feature and restore boundaries using a synthetic desktop."""
import unittest
import sqlite3
from contextlib import closing
from unittest import mock

import sync
import test_sync as fixtures


class Reconfiguration(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.StoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store

    def configure(self, mode, scope=None):
        candidate = self.store.propose(scope, mode)
        return self.store.reconfigure(candidate)

    def test_v1_same_choice_keeps_legacy_baseline_and_restore(self):
        self.store.update('sync')
        raw = self.store.enrollment.read_bytes()
        baseline = self.store.baseline.read_bytes()
        point = next(self.store.backups.glob('*.json'))
        v2 = {'version':2,'scope':'all','owners':[],'features':'both'}
        self.assertEqual(sync.policy_features(self.store.propose()),'both')
        self.assertEqual(self.store.reconfigure(v2)['state'],'configuration-unchanged')
        self.assertEqual(self.store.enrollment.read_bytes(),raw)
        self.assertEqual(self.store.baseline.read_bytes(),baseline)
        self.assertTrue(point.exists())
        self.assertEqual(self.store.update('restore')['state'],'restored')

    def test_live_configure_narrows_without_erasing_or_adopting_old_recovery(self):
        self.store.update('sync')
        old = self.store.backups
        raw = self.store.live.read_bytes()
        with mock.patch.object(self.store, 'is_offline',side_effect=AssertionError('app probe')):
            self.configure('history', self.fixture.source_id[:12])
        policy = self.store.policy()
        self.assertEqual((policy['owners'],sync.policy_features(policy)),([self.fixture.source_id],'history'))
        self.assertEqual(self.store.live.read_bytes(),raw)
        self.assertEqual(self.store.propose(),policy)
        self.assertTrue(any(old.glob('*.json')))
        self.assertNotEqual(self.store.backups,old)
        self.assertFalse(self.store.baseline.exists())
        with self.assertRaisesRegex(sync.Refusal,'no-recovery-point'):
            self.store.update('restore')
        self.store.update('sync')  # One verified participant is inert.
        self.assertEqual(self.store.live.read_bytes(),raw)
        fresh = sync.Store(self.fixture.root,self.fixture.codex,is_offline=lambda: True)
        self.assertEqual(fresh.backups,self.store.backups)

    def test_history_only_leaves_every_project_field_and_view_unchanged(self):
        self.configure('history')
        with closing(sqlite3.connect(self.fixture.codex/'state_5.sqlite')) as db:
            for table in ('projects','project_roots','project_idempotency_keys'):
                db.execute('DROP TABLE '+table)
            db.commit()
        original = self.fixture.read()
        target = original[sync.SCOPES]['inactive'][self.fixture.target_id]
        target['atoms'][sync.VIEW_PREFS]={'mode':'connection','chatSortMode':'updated_at'}
        self.fixture.write(original)
        self.store.update('sync')
        after = self.fixture.read()
        old_projects = sync.project_state(sync.profile_refs(original)[0],False,True)
        new_projects = sync.project_state(sync.profile_refs(after)[0],False,True)
        self.assertEqual(old_projects,new_projects)
        self.assertIn('prompt-history',self.fixture.target(after)['atoms'])
        base=sync.read_json(self.store.baseline.read_bytes())
        self.assertNotIn('projects',base)
        backup=sync.read_json(next(self.store.backups.glob('*.json')).read_bytes())
        self.assertTrue(all(n['part']=='atoms' and n['path'][0]=='prompt-history' for n in backup['changes']))
        self.assertEqual(self.store.update('restore')['state'],'restored')
        self.assertEqual(self.fixture.read(),original)

    def test_project_memberships_use_only_index_without_probing_recall_files(self):
        self.configure('projects')
        policy = self.store.policy()
        original = sync.Path.lstat
        def check_path(path):
            self.assertNotIn(self.fixture.codex/'sessions',path.parents)
            self.assertNotIn(self.fixture.codex/'archived_sessions',path.parents)
            return original(path)
        with mock.patch.object(sync.Path,'lstat',check_path):
            inventory = sync.policy_inventory(self.fixture.codex,policy)
            self.assertIn(fixtures.THREAD, inventory['threads'])
            self.assertFalse(inventory['history_threads'])
            self.store.update('sync')
        self.assertIn(fixtures.THREAD,self.fixture.target(self.fixture.read())['globals']['thread-project-assignments'])

    def _prepare_folders(self):
        state = self.fixture.read()
        state['thread-project-membership-host-ids'] = {fixtures.OTHER_THREAD:'ssh:fixture'}
        state['sidebar-project-thread-orders'] = {'legacy':{'threadIds':[fixtures.THREAD,fixtures.OTHER_THREAD]}}
        state['projectless-thread-ids'] = [fixtures.OTHER_THREAD]
        state[sync.ATOMS][sync.VIEW_PREFS] = {'mode':'connection'}
        target = self.fixture.target(state)
        target['atoms'][sync.VIEW_PREFS] = {'mode':'list','chatSortMode':'manual'}
        # A future shape in an excluded field must never reach the project reducer.
        target['globals']['thread-project-assignments'] = ['opaque', fixtures.OTHER_THREAD]
        target['globals']['sidebar-project-thread-orders'] = {'old':{'threadIds':[fixtures.OTHER_THREAD]}}
        self.fixture.write(state)
        return state

    def test_folders_excludes_chat_data_from_native_changes_baseline_receipt_and_inventory(self):
        self.configure('folders')
        before = self._prepare_folders()
        with closing(sqlite3.connect(self.fixture.codex/'state_5.sqlite')) as db:
            # Folder sync has no dependency on reading the chat table.
            db.execute('DROP TABLE threads')
            db.commit()
        self.store.update('sync')
        after = self.fixture.read()
        old_target = self.fixture.target(before)
        new_target = self.fixture.target(after)
        self.assertEqual(set(new_target['globals']) - set(old_target['globals']),sync.FOLDER_KEYS)
        for key in old_target['globals']:
            self.assertEqual(new_target['globals'][key],old_target['globals'][key])
        self.assertEqual(new_target['atoms'],old_target['atoms'])
        self.assertEqual(after[sync.ATOMS],before[sync.ATOMS])
        for file in [self.store.baseline,next(self.store.backups.glob('*.json'))]:
            raw = file.read_text()
            for excluded in (fixtures.THREAD,fixtures.OTHER_THREAD,fixtures.PROMPT,'ssh:fixture',
                             sync.VIEW_PREFS,'thread-project-assignments','sidebar-project-thread-orders'):
                self.assertNotIn(excluded, raw)
        point = sync.read_json(next(self.store.backups.glob('*.json')).read_bytes())
        self.assertTrue(all(n['part']=='globals' and n['path'][0] in sync.FOLDER_KEYS for n in point['changes']))
        self.store.update('restore')
        self.assertEqual(self.fixture.read(),before)

    def test_folders_plus_history_has_only_folders_and_recall(self):
        self.configure('folders-history')
        before = self._prepare_folders()
        del self.fixture.target(before)['atoms'][sync.VIEW_PREFS]
        self.fixture.write(before)
        self.store.update('sync')
        prev, target = self.fixture.target(before), self.fixture.target(self.fixture.read())
        self.assertEqual(set(target['globals'])-set(prev['globals']),sync.FOLDER_KEYS)
        self.assertNotIn(sync.VIEW_PREFS,target['atoms'])
        self.assertEqual(target['globals']['thread-project-assignments'],prev['globals']['thread-project-assignments'])
        self.assertIn(fixtures.PROMPT,repr(target['atoms']['prompt-history'][fixtures.THREAD]))
        receipt=sync.read_json(next(self.store.backups.glob('*.json')).read_bytes())
        allowed=sync.policy_fields(self.store.policy())
        self.assertTrue(all(c['path'][0] in allowed[c['part']] for c in receipt['changes']))
        self.store.update('restore')
        self.assertEqual(self.fixture.read(),before)

    def test_folder_receipts_cannot_adopt_full_project_consent_even_if_mislabeled(self):
        for mode in ('folders','folders-history'):
            policy=self.store.propose(features=mode)
            old=sync.make_policy(self.fixture.read(),'all',mode=='folders-history')
            with self.assertRaisesRegex(sync.Refusal,'enrollment-policy-mismatch'):
                sync.guard_scope({'policy':sync.fingerprint(sync.packed(old))},policy)
            for part, key in (('atoms',sync.VIEW_PREFS),('globals','projectless-thread-ids'),
                              ('globals','thread-project-assignments'),('globals','sidebar-project-thread-orders')):
                record={'policy':sync.fingerprint(sync.packed(policy)),'changes':[
                    {'owner':self.fixture.target_id,'part':part,'path':[key]}]}
                with self.assertRaisesRegex(sync.Refusal,'recovery-outside-enrollment'):
                    sync.guard_scope(record,policy)
            poisoned={'policy':sync.fingerprint(sync.packed(policy)),
                      'projects':{'canonical':{'t/'+fixtures.OTHER_THREAD:'alias'}}}
            with self.assertRaisesRegex(sync.Refusal,'recovery-outside-enrollment'):
                sync.guard_scope(poisoned,policy)

    def test_projects_only_leaves_history_and_off_does_not_touch_native(self):
        self.configure('projects')
        self.store.update('sync')
        self.assertNotIn('prompt-history',self.fixture.target(self.fixture.read())['atoms'])
        self.configure('off')
        old = self.store.live.read_bytes()
        observed = []
        original = sync.read_regular
        def guarded(path, *args):
            self.assertNotIn(self.fixture.codex, path.parents)
            observed.append(path)
            return original(path,*args)
        with mock.patch.object(sync,'read_regular',side_effect=guarded), \
             mock.patch.object(self.store,'is_offline',side_effect=AssertionError('app probe')), \
             mock.patch.object(sync,'local_inventory',side_effect=AssertionError('native DB probe')):
            self.assertEqual(self.store.propose(),self.store.policy())
            self.assertEqual(self.store.update('plan')['state'],'preview')
            self.assertEqual(self.store.update('sync')['state'],'off')
            with self.assertRaisesRegex(sync.Refusal,'restore-disabled'):
                self.store.update('restore')
        self.assertTrue(observed)
        self.assertEqual(self.store.live.read_bytes(),old)

    def test_reversion_never_reuses_old_epoch_and_rejects_backup_copy(self):
        self.configure('history')
        self.store.update('sync')
        first = self.store.backups
        point = next(first.glob('*.json'))
        self.configure('off')
        self.configure('history')
        second = self.store.backups
        self.assertNotEqual(first,second)
        self.assertFalse(any(second.iterdir()))
        self.store.update('sync')
        sync.replace(second/point.name,point.read_bytes())
        original = self.store.live.read_bytes()
        with self.assertRaisesRegex(sync.Refusal,'recovery-generation-mismatch'):
            self.store.update('restore')
        self.assertEqual(self.store.live.read_bytes(),original)

    def test_pending_and_failed_policy_commit_leave_original_policy_in_force(self):
        pol=self.store.policy()
        other=self.store.propose(features='history')
        sync.replace(self.store.pending,b'{}')
        with self.assertRaisesRegex(sync.Refusal,'pending-transaction'):
            self.store.reconfigure(other)
        self.store.pending.unlink()
        original=sync.replace
        def fail_pointer(path,raw):
            if path==self.store.enrollment:
                raise OSError('simulated write failure')
            return original(path,raw)
        with mock.patch.object(sync,'replace',side_effect=fail_pointer),self.assertRaises(OSError):
            self.store.reconfigure(other)
        self.assertEqual(self.store.policy(),pol)
        self.assertEqual(self.store.baseline.parent,self.store.data)

    def test_archive_cleanup_failure_preserves_committed_narrowing(self):
        with mock.patch.object(self.store, '_prune_generations', side_effect=OSError('busy')):
            result = self.configure('off')
        self.assertEqual(result['state'],'configured')
        self.assertEqual(result['archive_cleanup'],'deferred')
        self.assertEqual(sync.policy_features(self.store.policy()),'off')

    def test_forged_changes_outside_feature_and_selected_identity_refused(self):
        pol=self.store.propose(self.fixture.source_id[:12],'history')
        item={'owner':self.fixture.target_id,'part':'atoms','path':['prompt-history']}
        for change in (item,dict(item,owner=self.fixture.source_id,part='globals',path=['local-projects']),
                       dict(item,owner=self.fixture.source_id,path=[sync.VIEW_PREFS])):
            record={'policy':sync.fingerprint(sync.packed(pol)),'changes':[change]}
            with self.assertRaisesRegex(sync.Refusal,'recovery-outside-enrollment'):
                sync.guard_scope(record,pol)

    def test_retained_generations_are_private_bounded_and_legacy_untouched(self):
        self.store.update('sync')
        legacy=self.store.backups
        for i in range(2*sync.MAX_ARCHIVED_GENERATIONS+1):
            self.configure('history' if i%2==0 else 'off')
        dirs=list(self.store.generations.iterdir())
        self.assertEqual(len(dirs),1+sync.MAX_ARCHIVED_GENERATIONS)
        for p in dirs:
            self.assertEqual(p.stat().st_mode & 0o777,0o700)
            self.assertEqual((p/'enrollment.json').stat().st_mode & 0o777,0o600)
        self.assertTrue(any(legacy.glob('*.json')))


if __name__=='__main__':
    unittest.main()
