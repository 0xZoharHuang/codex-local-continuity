import copy
import json
import unittest
from uuid import UUID

try:
    from .history import sync_history
except ImportError:
    from history import sync_history


TID = str(UUID(int=1))
OTHER = str(UUID(int=2))
INVENTORY = {'threads': {TID}}


def profile(records=None):
    h = {} if records is None else {TID: copy.deepcopy(records)}
    return {'atoms': {'prompt-history': h, 'other-setting': 'keep'}, 'globals': {'unchanged': True}}


def history(p):
    return p['atoms']['prompt-history'][TID]


def txt(value):
    return {'plain': value}


def doc(value, marks=None, extra=None):
    node = {'type': 'text', 'text': value}
    if marks is not None:
        node['marks'] = marks
    inner = [node] + ([] if extra is None else [extra])
    return {'markdown': value, 'document': {'type': 'doc', 'content': [{'type': 'paragraph', 'content': inner}]}}


class TestHistory(unittest.TestCase):
    def test_overlapping_cold_windows_keep_observed_repetitions_once(self):
        first = doc('a', [{'type': 'literalPaste'}])
        a = [first, txt('a'), txt('b')]
        group = {'short': profile([txt('a'), txt('b')]), 'long': profile(a), 'empty': profile()}
        state, _ = sync_history(group, None, INVENTORY)
        self.assertEqual(history(group['long']), a)
        for name in ('short','empty'):
            self.assertEqual(history(group[name]), [txt('a'), txt('a'), txt('b')])
        snapshot = copy.deepcopy(group)
        again, counts = sync_history(group,state,INVENTORY)
        self.assertEqual(again,state)
        self.assertEqual(group,snapshot)
        self.assertEqual(counts['entries_copied'],0)

    def test_new_owner_adopts_old_overlapping_records_without_losing_new_sends(self):
        group = {'a': profile([txt('same'),txt('tail')]), 'b': profile()}
        state, _ = sync_history(group, None, INVENTORY)
        history(group['a']).append(txt('same'))
        history(group['b']).append(txt('same'))
        before = [doc('same'),doc('tail')]
        group['joined'] = profile(before)
        state, _ = sync_history(group,state,INVENTORY)
        self.assertEqual(history(group['joined']),before + [txt('same'),txt('same')])
        for name in ('a','b'):
            self.assertEqual(history(group[name]),[txt('same'),txt('tail'),txt('same'),txt('same')])
        group['next'] = profile([txt('tail'),txt('same')])
        state, _ = sync_history(group,state,INVENTORY)
        self.assertEqual(history(group['next']).count(txt('same')),3)
        again, counts = sync_history(group,state,INVENTORY)
        self.assertEqual(again,state)
        self.assertEqual(counts['entries_copied'],0)

    def test_partially_shared_anchor_and_conflicting_cold_order(self):
        group = {'a': profile([txt('x'),txt('middle')]), 'b': profile([txt('middle'),txt('y')]),
                 'c': profile()}
        sync_history(group,None,INVENTORY)
        for p in group.values():
            self.assertEqual(history(p), [txt('x'),txt('middle'),txt('y')])
        first, second = [txt('a'),txt('b')], [txt('b'),txt('a')]
        group = {'a':profile(first),'b':profile(second)}
        sync_history(group,None,INVENTORY)
        self.assertEqual(history(group['a']),first)
        self.assertEqual(history(group['b']),second)

    def test_enrollment_and_privacy(self):
        first = doc('private sample', [{'type': 'literalPaste'}])
        opaque = doc('private sample', [{'type': 'redactedSecret', 'attrs': {'hidden': 'do not expose'}}])
        group = {'a': profile([first, opaque, txt('repeat'), txt('repeat')]), 'b': profile([txt('own')])}
        state, stats = sync_history(group, None, INVENTORY)
        self.assertEqual(history(group['a']), [txt('own'), first, opaque, txt('repeat'), txt('repeat')])
        self.assertEqual(history(group['b']), [txt('private sample'), txt('repeat'), txt('repeat'), txt('own')])
        self.assertEqual(stats['opaque_records'], 1)
        encoded = json.dumps(state)
        for fragment in ('private sample', 'redactedSecret', 'do not expose', 'repeat', TID):
            self.assertNotIn(fragment, encoded)
        same = copy.deepcopy(group)
        next_state, counts = sync_history(group, state, INVENTORY)
        self.assertEqual(group, same)
        self.assertEqual(next_state, state)
        self.assertEqual(counts['entries_copied'], 0)

    def test_same_payload_in_separate_new_submissions(self):
        group = {'a': profile([txt('first')]), 'b': profile(), 'c': profile()}
        state, _ = sync_history(group, None, INVENTORY)
        history(group['b']).append(txt('again'))
        history(group['c']).append(txt('again'))
        state, stats = sync_history(group, state, INVENTORY)
        for p in group.values():
            self.assertEqual(history(p), [txt('first'), txt('again'), txt('again')])
        history(group['a']).append(txt('again'))
        state, _ = sync_history(group, state, INVENTORY)
        self.assertTrue(all(history(p).count(txt('again')) == 3 for p in group.values()))
        next_state, stats = sync_history(group, state, INVENTORY)
        self.assertEqual(state, next_state)
        self.assertEqual(stats['entries_copied'], 0)

    def test_ring_rotation_and_target_opaque(self):
        opaque = {'markdown': 'local', 'document': {'type': 'file', 'attrs': {'path': '/only/here'}}}
        records = [txt('event '+str(i)) for i in range(20)]
        group = {'a': profile(records), 'b': profile([opaque])}
        state, _ = sync_history(group, None, INVENTORY)
        self.assertEqual(history(group['b'])[-1], opaque)
        self.assertEqual(len(history(group['b'])), 20)
        group['a']['atoms']['prompt-history'][TID] = history(group['a'])[2:] + [txt('new'), txt('new')]
        state, stats = sync_history(group, state, INVENTORY)
        self.assertIn(opaque, history(group['b']))
        self.assertEqual(history(group['b'])[-2:], [txt('new'),txt('new')])
        self.assertEqual(stats['aged_out'], 2)
        self.assertEqual(len(history(group['b'])), 20)
        self.assertEqual(history(group['a'])[-2:], [txt('new'),txt('new')])
        next_state, counts = sync_history(group, state, INVENTORY)
        self.assertEqual(next_state, state)
        self.assertEqual(counts['entries_copied'], 0)

    def test_untrusted_shapes_sensitive_references_and_excluded_scope(self):
        bad = [doc('label', [{'type':'link','attrs':{'href':'private'}}]), doc('label',extra={'type':'mention'}),
               txt('Bearer this_substitute_contains_40_chars'), {'plain':'x','attachment':{}},
               {'markdown':'different','document':doc('visible')['document']}]
        group={'a':profile(bad+[txt('clean')]),'b':profile()}
        src=group['a']['atoms']['prompt-history']
        for k in ('global','chatgpt-global',OTHER,'remote:'+TID):
            src[k]=[txt('other scope')]
        before=copy.deepcopy(src)
        _, stats=sync_history(group,None,INVENTORY)
        self.assertEqual(src,before)
        self.assertEqual(group['b']['atoms']['prompt-history'],{TID:[txt('clean')]})
        self.assertEqual(stats['opaque_records'],len(bad))

    def test_scope_caps_empty_clear_and_shape(self):
        group={'a':profile([txt('one')]),'b':profile(),'c':profile(),'d':profile()}
        other={str(UUID(int=k)):[txt('x')] for k in range(5,105)}
        group['b']['atoms']['prompt-history']={'global':[],**other}
        group['c']['atoms']['prompt-history']=['legacy global']
        state,stats=sync_history(group,None,INVENTORY)
        self.assertEqual(group['b']['atoms']['prompt-history'],{'global':[],**other})
        self.assertEqual(group['c']['atoms']['prompt-history'],['legacy global'])
        del group['d']['atoms']['prompt-history'][TID]
        sync_history(group,state,INVENTORY)
        self.assertNotIn(TID,group['d']['atoms']['prompt-history'])

    def test_clear_keeps_old_occurrences_out_while_future_sends_continue(self):
        group = {'a': profile([txt('historic')]), 'b': profile()}
        state, _ = sync_history(group, None, INVENTORY)
        history(group['b']).clear()
        state, _ = sync_history(group, state, INVENTORY)
        self.assertEqual(history(group['b']), [])
        history(group['a']).append(txt('new'))
        state, _ = sync_history(group, state, INVENTORY)
        self.assertEqual(history(group['b']), [txt('new')])

    def test_nonconsecutive_clear_with_equal_neighbors_is_never_a_new_send(self):
        original = [txt(v) for v in ('repeat','removed-1','repeat','removed-2','repeat')]
        group = {'a':profile(original), 'b':profile()}
        baseline, _ = sync_history(group,None,INVENTORY)
        history(group['b'])[:] = [txt('repeat')] * 3
        baseline, stats = sync_history(group,baseline,INVENTORY)
        self.assertEqual(stats['entries_copied'],0)
        self.assertEqual(history(group['a']),original)
        self.assertEqual(history(group['b']),[txt('repeat')]*3)
        history(group['b']).append(txt('repeat'))
        baseline, _ = sync_history(group,baseline,INVENTORY)
        self.assertEqual(history(group['a']),original + [txt('repeat')])
        next_baseline, stats = sync_history(group,baseline,INVENTORY)
        self.assertEqual(next_baseline,baseline)
        self.assertEqual(stats['entries_copied'],0)

    def test_plain_with_native_document_requires_exact_safe_visible_text(self):
        safe = {'plain':'visible', 'document':doc('visible',[{'type':'literalPaste'}])['document']}
        mismatch = {'plain':'other', 'document':doc('visible')['document']}
        ref = {'plain':'label', 'document':doc('label',[{'type':'link','attrs':{'href':'hidden'}}])['document']}
        opaque = copy.deepcopy(safe)
        opaque['document']['attrs']={'futureOpaqueReference':'hidden'}
        group = {'a':profile([safe,mismatch,ref,opaque]), 'b':profile()}
        _, stats=sync_history(group,None,INVENTORY)
        self.assertEqual(history(group['a']),[safe,mismatch,ref,opaque])
        self.assertEqual(history(group['b']),[{'plain':'visible'}])
        self.assertEqual(stats['opaque_records'],3)

    def test_first_seen_preserves_native_order_and_does_not_overfill(self):
        a=[txt('aa'+str(i)) for i in range(20)]
        b=[txt('bb'+str(i)) for i in range(20)]
        group={'a':profile(a),'b':profile(b),'c':profile()}
        state,stats=sync_history(group,None,INVENTORY)
        self.assertEqual(history(group['a']),a)
        self.assertEqual(history(group['b']),b)
        self.assertEqual(len(history(group['c'])),20)
        self.assertEqual(group['a']['globals'],{'unchanged':True})
        next_state,counts=sync_history(group,state,INVENTORY)
        self.assertEqual(next_state,state)
        self.assertEqual(counts['entries_copied'],0)


if __name__ == '__main__':
    unittest.main()
