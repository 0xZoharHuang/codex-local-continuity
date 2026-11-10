import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import settings_ui


class Reader:
    def __init__(self, base, enrolled=True):
        self.enrollment = base/'policy.json'
        self.ids = ['1' * 64, 'a' * 64]
        self.current = {'scope': 'selected', 'owners': [self.ids[0]], 'mode': 'projects'}
        if enrolled:
            self.enrollment.write_text('original')
        self.calls = []

    def list_profiles(self):
        return [{'label': o[:12], 'active': i == 0} for i, o in enumerate(self.ids)]

    def policy(self):
        assert self.enrollment.is_file()
        return self.current

    def propose(self, selection, features):
        self.calls.append(('propose', selection, features))
        if selection is None:
            return dict(self.current, mode=features)
        if selection == 'all':
            return {'scope': 'all', 'owners': [], 'mode': features}
        owners = self.ids if selection == 'existing' else [o for o in self.ids if o[:12] in selection.split(',')]
        return {'scope': 'selected', 'owners': owners, 'mode': features}

    def update(self, action, proposal):
        self.calls.append((action, proposal))
        assert action == 'plan'
        return {'counts': {'profiles': 2, 'changed_fields': 3, 'projects': {'projects': 4, 'conflicts': 0},
                           'history': {'entries_copied': 7}}}


class EditorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        features = patch.object(settings_ui.sync, 'policy_features', lambda p: p['mode'], create=True)
        features.start()
        self.addCleanup(features.stop)

    def run_editor(self, store, answers, lang='en'):
        log = []
        inputs = iter(answers)
        def read(prompt):
            log.append(prompt)
            try:
                return next(inputs)
            except StopIteration:
                raise EOFError
        value = settings_ui.edit(store, lang=lang, input_fn=read, output=log.append)
        return value, '\n'.join(log)

    def test_keep_owners_preview_then_confirm(self):
        store = Reader(self.base)
        proposal, report = self.run_editor(store, ['', '5', 'Y'])
        self.assertEqual(proposal, dict(store.current, mode='history'))
        self.assertEqual(store.calls[0], ('propose', None, 'history'))
        self.assertEqual(store.calls[1], ('plan', proposal))
        self.assertIn('History entries to copy · 7', report)
        self.assertIn('(active)', report)
        self.assertNotIn(store.ids[0], report)
        self.assertLess(report.index('Read-only preview'), report.index('Confirm this exact proposal'))
        self.assertEqual(store.enrollment.read_text(), 'original')

    def test_new_install_explicit_all_and_off_only_previews(self):
        store = Reader(self.base, enrolled=False)
        proposal, report = self.run_editor(store, ['3', '6', '确认'], 'zh-CN')
        self.assertEqual(proposal, {'scope':'all', 'owners':[], 'mode':'off'})
        self.assertIn('此次预览不会安装工具', report)
        self.assertIn('只读预览', report)
        self.assertIn('doctor', report)
        self.assertFalse(store.enrollment.exists())

    def test_folder_choices_explain_existing_access_before_confirmation(self):
        store = Reader(self.base)
        for number, feature, language, wording in [
                ('1', 'folders', 'en', 'profile can already access'),
                ('2', 'folders-history', 'zh', '当前身份本来就能访问')]:
            with self.subTest(feature=feature):
                proposal, report = self.run_editor(store, ['', number, 'y'], language)
                self.assertEqual(proposal['mode'], feature)
                self.assertEqual(store.calls[-2], ('propose', None, feature))
                self.assertIn(wording, report)

    def test_codex_not_initialized_yet(self):
        store = Reader(self.base, enrolled=False)
        with patch.object(store, 'list_profiles', side_effect=FileNotFoundError):
            proposal, report = self.run_editor(store, [])
        self.assertIsNone(proposal)
        self.assertIn('Open Codex and sign in', report)
        self.assertEqual(store.calls, [])

    def test_pasted_subset_retries_bad_input(self):
        store = Reader(self.base)
        labels = [o[:12] for o in store.ids]
        proposal, report = self.run_editor(store, ['99','3','not-an-identity',','.join([labels[0]]*2),
                                                    labels[1], '4', 'yes'])
        self.assertEqual(proposal['owners'], [store.ids[1]])
        self.assertEqual(proposal['mode'], 'both')
        self.assertNotIn('not-an-identity', report)

    def test_cancel_eof_and_blank_confirmation_never_commit(self):
        store = Reader(self.base)
        for answers in (['q'], [], ['', 'q'], ['', '1', ''], ['', '1', 'n']):
            with self.subTest(answers=answers):
                result, report = self.run_editor(store, answers)
                self.assertIsNone(result)
                self.assertIn('Cancelled', report)
        self.assertTrue(all(c[0] in ('plan', 'propose') for c in store.calls))
        self.assertEqual(store.enrollment.read_text(), 'original')


if __name__ == '__main__':
    unittest.main()
