import json
import unittest
from copy import deepcopy

import recall
from history import ATOM, CAP


SCOPE = '10000000-0000-4000-8000-000000000001'
OTHER = '10000000-0000-4000-8000-000000000002'
OWNERS = {'a', 'b'}
ALLOWED = {SCOPE, OTHER}


def state(history):
    return {recall.ATOMS: {ATOM: history, 'composer-drafts': {'private': 'untouched'}},
            'local-projects': {'opaque': True}, 'electron-renderer-content-owner': 'identity'}


def texts(values):
    return [v if isinstance(v, str) else v.get('plain') for v in values]


class RecallTests(unittest.TestCase):
    def test_capture_then_reset_new_b_restore_and_same_b_restart(self):
        a = state({SCOPE: ['repeat', 'repeat']})
        before = deepcopy(a)
        ledger, report = recall.capture(a, None, OWNERS, 'a', ALLOWED)
        self.assertEqual(a, before)
        self.assertEqual(report['captured_entries'], 2)
        reset = state({})
        ledger, _ = recall.capture(reset, ledger, OWNERS, None, ALLOWED)
        ledger, report = recall.merge(reset, ledger, OWNERS, 'b', ALLOWED)
        self.assertEqual(texts(reset[recall.ATOMS][ATOM][SCOPE]), ['repeat', 'repeat'])
        saved = deepcopy((reset, ledger))
        ledger, _ = recall.merge(reset, ledger, OWNERS, 'b', ALLOWED)
        self.assertEqual(saved, (reset, ledger))
        reset_again = state({SCOPE: []})
        ledger, _ = recall.merge(reset_again, ledger, OWNERS, 'b', ALLOWED)
        self.assertEqual(texts(reset_again[recall.ATOMS][ATOM][SCOPE]), ['repeat', 'repeat'])

    def test_post_merge_equal_independent_sends_and_roundtrip(self):
        a = state({SCOPE: ['base']})
        ledger, _ = recall.merge(a, None, OWNERS, 'a', ALLOWED)
        b = state({})
        ledger, _ = recall.merge(b, ledger, OWNERS, 'b', ALLOWED)
        a[recall.ATOMS][ATOM][SCOPE].append('again')
        ledger, _ = recall.capture(a, ledger, OWNERS, 'a', ALLOWED)
        b[recall.ATOMS][ATOM][SCOPE].append('again')
        ledger, _ = recall.capture(b, ledger, OWNERS, 'b', ALLOWED)
        ledger, _ = recall.merge(b, ledger, OWNERS, 'b', ALLOWED)
        self.assertEqual(texts(b[recall.ATOMS][ATOM][SCOPE]), ['base', 'again', 'again'])
        ledger, _ = recall.merge(a, ledger, OWNERS, 'a', ALLOWED)
        self.assertEqual(texts(a[recall.ATOMS][ATOM][SCOPE]), ['base', 'again', 'again'])
        old = deepcopy((a, ledger))
        ledger, _ = recall.merge(a, ledger, OWNERS, 'a', ALLOWED)
        self.assertEqual(old, (a, ledger))

    def test_native_opaque_rich_records_and_noneligible_scopes_preserved(self):
        rich = {'document': {'type': 'doc', 'content': [{'type':'paragraph','content':[
            {'type':'text','text':'original'}]}]}, 'plain':'original'}
        opaque = {'document': {'type':'unknown','secret':'never-cache'}, 'plain':'hidden'}
        a = state({SCOPE:['peer'], 'global':['global-private'], 'cloud-id':['other-private']})
        ledger, _ = recall.capture(a, None, OWNERS, 'a', ALLOWED)
        b = state({SCOPE:[rich, opaque], 'chatgpt-global':['cloud-private']})
        before = deepcopy(b)
        ledger, _ = recall.merge(b, ledger, OWNERS, 'b', ALLOWED)
        vals = b[recall.ATOMS][ATOM][SCOPE]
        self.assertEqual(vals[-2:], [rich, opaque])
        self.assertEqual(b[recall.ATOMS]['composer-drafts'], before[recall.ATOMS]['composer-drafts'])
        self.assertEqual(b['local-projects'], before['local-projects'])
        self.assertEqual(b[recall.ATOMS][ATOM]['chatgpt-global'], ['cloud-private'])
        cache = json.dumps(ledger)
        for secret in ('never-cache','global-private','other-private','cloud-private','untouched'):
            self.assertNotIn(secret, cache)
        previous = deepcopy((b, ledger))
        ledger, _ = recall.merge(b, ledger, OWNERS, 'b', ALLOWED)
        self.assertEqual(previous, (b, ledger))

    def test_bounds_unenrolled_and_unsupported_windows(self):
        data = state({SCOPE:['too-many']*(CAP+1), OTHER:[{'reference':'opaque'}], 'global':['private']})
        ledger, _ = recall.capture(data, None, OWNERS, 'a', ALLOWED)
        self.assertEqual(ledger['histories']['a'], {})
        original = deepcopy(data)
        ledger, _ = recall.merge(data, ledger, OWNERS, 'b', ALLOWED)
        self.assertEqual(data, original)
        old = deepcopy(ledger)
        ledger, _ = recall.capture(state({SCOPE:['unselected']}), ledger, OWNERS, 'c', ALLOWED)
        self.assertEqual(old, ledger)
        with self.assertRaises(ValueError):
            recall.capture(data, ledger, {'a'}, 'a', ALLOWED)

    def test_sequential_captures_preserve_equal_sends_before_any_merge(self):
        a = state({SCOPE: ['one']})
        ledger, _ = recall.merge(a, None, OWNERS, 'a', ALLOWED)
        for value in ['repeat','repeat','last']:
            a[recall.ATOMS][ATOM][SCOPE].append(value)
            ledger, _ = recall.capture(a, ledger, OWNERS, 'a', ALLOWED)
        old = deepcopy(ledger)
        ledger, counts = recall.capture(a, ledger, OWNERS, 'a', ALLOWED)
        self.assertEqual(old, ledger)
        self.assertEqual(counts['observed_sends'], 0)
        b = state({})
        ledger, _ = recall.merge(b, ledger, OWNERS, 'b', ALLOWED)
        self.assertEqual(texts(b[recall.ATOMS][ATOM][SCOPE]), ['one','repeat','repeat','last'])

    def test_rotating_window_and_opaque_capacity(self):
        a = state({SCOPE: [str(i) for i in range(CAP)]})
        ledger, _ = recall.merge(a, None, OWNERS, 'a', ALLOWED)
        b = state({})
        ledger, _ = recall.merge(b, ledger, OWNERS, 'b', ALLOWED)
        a[recall.ATOMS][ATOM][SCOPE] = [str(i) for i in range(1, CAP + 1)]
        ledger, _ = recall.capture(a, ledger, OWNERS, 'a', ALLOWED)
        ledger, _ = recall.merge(b, ledger, OWNERS, 'b', ALLOWED)
        self.assertEqual(texts(b[recall.ATOMS][ATOM][SCOPE]), [str(i) for i in range(1, CAP + 1)])
        opaque = state({OTHER: [{'unknown':i} for i in range(CAP)]})
        before = deepcopy(opaque)
        ledger, _ = recall.capture(state({OTHER:['safe']}), ledger, OWNERS, 'a', ALLOWED)
        ledger, _ = recall.merge(opaque, ledger, OWNERS, 'b', ALLOWED)
        self.assertEqual(opaque[recall.ATOMS][ATOM][OTHER], before[recall.ATOMS][ATOM][OTHER])

    def test_scope_limit_and_native_credentials_remain_private(self):
        scopes = {f'{i:08x}-0000-4000-8000-000000000001' for i in range(105)}
        hist = {s:['sample'] for s in sorted(scopes)}
        hist[SCOPE] = ['api_key=abcdef1234567890']
        a = state(hist)
        original = deepcopy(a)
        ledger, _ = recall.capture(a, None, OWNERS, 'a', scopes | ALLOWED)
        self.assertLessEqual(len(ledger['histories']['a']), 100)
        self.assertNotIn('abcdef1234567890', json.dumps(ledger))
        self.assertEqual(a, original)
        b = state({})
        ledger, _ = recall.merge(b, ledger, OWNERS, 'b', scopes | ALLOWED)
        self.assertLessEqual(len(b[recall.ATOMS][ATOM]), 100)

    def test_seed_latest_only_empty_scope_current_wins(self):
        current = state({SCOPE:['current']})
        ledger, _ = recall.capture(current, None, OWNERS, 'a', ALLOWED)
        def receipt(owner, scope, body):
            return {'changes':[{'owner':owner,'part':'atoms','path':[ATOM,scope],'before':[body]}]}
        ledger = recall.seed(ledger, [receipt('a',SCOPE,['old']), receipt('b',SCOPE,['latest','latest']),
            receipt('b',SCOPE,['ancient']), receipt('c',OTHER,['unselected']),
            receipt('a','global',['global-private'])], OWNERS, ALLOWED)
        self.assertEqual(texts(ledger['histories']['a'][SCOPE]), ['current'])
        self.assertEqual(texts(ledger['histories']['b'][SCOPE]), ['latest','latest'])
        before = deepcopy(ledger)
        ledger = recall.seed(ledger, [receipt('b',SCOPE,['latest','latest'])], OWNERS, ALLOWED)
        self.assertEqual(before, ledger)


if __name__ == '__main__':
    unittest.main()
