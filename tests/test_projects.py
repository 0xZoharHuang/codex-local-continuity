import unittest
from copy import deepcopy
from projects import (ASSIGN, CHAT_ORDER, HOSTS, MAPPING, ORDER, PROJECTS, UNASSIGN,
                      sync_projects)


HOST = 'local:/device/.codex'
PROJECT = {'p': {'name': 'Alpha', 'roots': ['/work/alpha']},
           'q': {'name': 'Beta', 'roots': ['/work/beta']}}
ALIAS = {'a': 'p', 'b': 'q'}


def obj(alias, name, root):
    return {'id': alias, 'name': name, 'rootPaths': [root], 'createdAt': 123, 'updatedAt': 456}


def ref(alias):
    return {'projectKind': 'local', 'projectId': alias}


class Projects(unittest.TestCase):
    def setUp(self):
        self.inv = {'host': HOST, 'projects': deepcopy(PROJECT), 'aliases': dict(ALIAS),
                    'all_project_ids': set(PROJECT), 'threads': {'t1', 't2', 't3', 'outside-cwd'}}
        src = {PROJECTS: {'a': obj('a', 'Alpha', '/work/alpha'), 'b': obj('b', 'Beta', '/work/beta')},
               MAPPING: {HOST: dict(ALIAS)}, ASSIGN: {'outside-cwd': ref('a')}, UNASSIGN: ['t2'],
               HOSTS: {'outside-cwd': 'local'}, ORDER: ['a', 'b'], CHAT_ORDER: {'a': {'threadIds': ['outside-cwd']}}}
        self.profiles = {o: {'globals': g, 'atoms': {'never-share': o}}
                         for o, g in [('A', src), ('B', {}), ('C', {})]}
        self.base = None

    def sync(self):
        self.base, counts = sync_projects(self.profiles, self.base, self.inv)
        return counts

    def g(self, owner):
        return self.profiles[owner]['globals']

    def test_initial_and_enroll(self):
        self.assertEqual(self.sync()['projects'], 2)
        self.assertEqual(self.g('B')[ASSIGN]['outside-cwd'], ref('a'))
        self.assertEqual(self.g('C')[UNASSIGN], ['t2'])
        self.assertEqual(self.g('B')[MAPPING][HOST], ALIAS)
        self.profiles['D'] = {'globals': {ORDER: []}, 'atoms': {}}
        self.assertEqual(self.sync()['conflicts'], 0)
        self.assertEqual(self.g('D')[ORDER], ['a', 'b'])
        before = deepcopy((self.base, self.profiles))
        self.sync()
        self.assertEqual(before, (self.base, self.profiles))

    def test_create_rename_move_reorder(self):
        self.sync()
        self.inv['projects']['p']['name'] = 'Renamed'
        self.g('B')[PROJECTS]['a']['name'] = 'Renamed'
        self.g('C')[ORDER] = ['b', 'a']
        self.g('B')[ASSIGN]['outside-cwd'] = ref('b')
        self.g('B')[UNASSIGN].remove('t2')
        self.g('B')[ASSIGN]['t2'] = ref('a')
        self.inv['projects']['r'] = {'name': 'Gamma', 'roots': ['/work/gamma']}
        self.inv['all_project_ids'].add('r')
        self.inv['aliases']['c'] = 'r'
        self.g('B')[PROJECTS]['c'] = obj('c', 'Gamma', '/work/gamma')
        self.assertEqual(self.sync()['conflicts'], 0)
        for owner in self.profiles:
            g = self.g(owner)
            self.assertEqual(g[PROJECTS]['a']['name'], 'Renamed')
            self.assertIn('c', g[PROJECTS])
            self.assertEqual(g[ASSIGN]['outside-cwd'], ref('b'))
            self.assertEqual(g[ASSIGN]['t2'], ref('a'))
            self.assertNotIn('t2', g[UNASSIGN])
            self.assertEqual(g[ORDER], ['b', 'a'])
        self.g('C')[ASSIGN].pop('outside-cwd');self.g('C')[UNASSIGN].append('outside-cwd')
        self.sync()
        self.assertNotIn('outside-cwd', self.g('A')[ASSIGN])
        self.assertIn('outside-cwd', self.g('A')[UNASSIGN])

    def test_conflict_survives_and_converges(self):
        self.sync()
        self.g('B')[ASSIGN]['outside-cwd'] = ref('b')
        del self.g('C')[ASSIGN]['outside-cwd']
        self.g('C')[UNASSIGN].append('outside-cwd')
        self.assertEqual(self.sync()['conflicts'], 1)
        self.assertEqual(self.sync()['conflicts'], 1)
        self.assertEqual(self.g('A')[ASSIGN]['outside-cwd'], ref('a'))
        self.assertEqual(self.g('B')[ASSIGN]['outside-cwd'], ref('b'))
        self.g('C')[UNASSIGN].remove('outside-cwd')
        self.g('C')[ASSIGN]['outside-cwd'] = ref('b')
        self.assertEqual(self.sync()['conflicts'], 0)
        self.assertEqual(self.g('A')[ASSIGN]['outside-cwd'], ref('b'))

    def test_delayed_sql_rename_and_independent_root_update(self):
        self.sync()
        self.g('C')[PROJECTS]['a']['name'] = 'New name'
        self.assertEqual(self.sync()['conflicts'], 1)
        self.assertEqual(self.g('C')[PROJECTS]['a']['name'], 'New name')
        self.inv['projects']['p']['name'] = 'New name'
        self.assertEqual(self.sync()['conflicts'], 0)
        self.assertEqual(self.g('A')[PROJECTS]['a']['name'], 'New name')
        self.g('A')[PROJECTS]['a']['name'] = 'Next name'
        self.g('B')[PROJECTS]['a']['rootPaths'] = ['/new']
        self.inv['projects']['p'] = {'name': 'Next name', 'roots': ['/new']}
        self.assertEqual(self.sync()['conflicts'], 0)
        for owner in self.profiles:
            self.assertEqual(self.g(owner)[PROJECTS]['a']['rootPaths'], ['/new'])
            self.assertEqual(self.g(owner)[PROJECTS]['a']['name'], 'Next name')

    def test_confirmed_delete_only(self):
        self.sync()
        self.g('B')[PROJECTS].pop('a')
        self.g('B')[MAPPING][HOST].pop('a')
        self.inv['projects'].pop('p')  # Now cloud/ineligible, still exists in native store.
        self.assertEqual(self.sync()['confirmed_deletions'], 0)
        self.assertIn('a', self.g('A')[PROJECTS])
        # A continued lack of a definition does not retroactively prove a deletion.
        self.inv['projects']['p'] = deepcopy(PROJECT['p'])
        self.sync()
        self.g('B')[PROJECTS].pop('a')
        self.g('B')[MAPPING][HOST].pop('a')
        self.inv['all_project_ids'].remove('p')
        self.inv['projects'].pop('p')
        self.assertEqual(self.sync()['confirmed_deletions'], 1)
        self.assertEqual(self.sync()['confirmed_deletions'], 1)
        for o in self.profiles:
            self.assertNotIn('a', self.g(o)[PROJECTS])
            self.assertNotIn('a', self.g(o)[ORDER])
            self.assertNotIn('a', self.g(o)[CHAT_ORDER])
            self.assertNotIn('outside-cwd', self.g(o)[ASSIGN])

    def test_opaque_records_protected_and_incomplete_owner(self):
        foreign = {'id': 'z', 'name': 'Private', 'rootPaths': ['/private'],
                   'chatGptBacking': {'secret': 'never-copy'}}
        self.g('B')[PROJECTS] = {'z': deepcopy(foreign)}
        self.g('B')[MAPPING] = {'ssh:elsewhere': {'z': 'foreign'}}
        self.g('B')[ASSIGN] = {'outside-cwd': {'projectKind': 'remote', 'projectId': 'z'}}
        self.g('B')[ORDER] = ['z']
        self.g('B')['selected-project'] = {'secret': True}
        self.sync()
        self.assertEqual(self.g('B')[PROJECTS]['z'], foreign)
        self.assertNotIn('z', self.g('A')[PROJECTS])
        self.assertEqual(self.g('B')[ASSIGN]['outside-cwd']['projectKind'], 'remote')
        self.assertNotIn('never-copy', repr(self.base))
        self.assertNotIn('never-share', repr(self.base))
        self.assertNotIn('ssh:elsewhere', repr(self.base))
        before = deepcopy(self.g('B'))
        del self.g('B')[PROJECTS]; del before[PROJECTS]
        self.assertEqual(self.sync()['partial_profiles'], 1)
        self.assertEqual(self.g('B'), before)

    def test_remote_host_hint_without_assignment_protects_chat_order_on_both_sides(self):
        self.g('A')[HOSTS]['t1'] = 'ssh:source'
        self.g('A')[CHAT_ORDER]['b'] = {'threadIds': ['t1', 't3']}
        self.g('B')[HOSTS] = {'t3': 'ssh:destination'}
        self.sync()
        self.assertEqual(self.g('A')[CHAT_ORDER]['b']['threadIds'], ['t1', 't3'])
        self.assertEqual(self.g('C')[CHAT_ORDER]['b']['threadIds'], ['t3'])
        self.assertNotIn('b', self.g('B').get(CHAT_ORDER, {}))
        self.assertEqual(self.base['canonical']['s/q'], ['t3'])
        self.assertNotIn('ssh:', repr(self.base))
        before = deepcopy(self.profiles)
        self.sync()
        self.assertEqual(self.profiles, before)

    def test_all_empty_transient_and_empty_order(self):
        self.sync()
        for k, v in list(self.g('B').items()):
            self.g('B')[k] = type(v)()
        empty = deepcopy(self.g('B'))
        self.assertEqual(self.sync()['partial_profiles'], 1)
        self.assertEqual(self.g('B'), empty)
        self.assertEqual(self.g('A')[ORDER], ['a', 'b'])
        self.g('C')[ORDER] = []
        self.g('C')[CHAT_ORDER]['a']['threadIds'] = []
        self.sync()
        self.assertEqual(self.g('A')[ORDER], ['a', 'b'])
        self.assertEqual(self.g('A')[CHAT_ORDER]['a']['threadIds'], ['outside-cwd'])

    def test_recreated_native_id_and_alias(self):
        self.sync()
        self.g('B')[PROJECTS].pop('a')
        self.g('B')[MAPPING][HOST].pop('a')
        self.inv['all_project_ids'].remove('p')
        del self.inv['projects']['p']
        self.assertEqual(self.sync()['confirmed_deletions'], 1)
        self.inv['all_project_ids'].add('p')
        self.inv['projects']['p'] = {'name': 'Recreated', 'roots': ['/restored']}
        self.inv['aliases']['restored'] = 'p'
        self.g('C')[PROJECTS]['restored'] = obj('restored', 'Recreated', '/restored')
        self.assertEqual(self.sync()['confirmed_deletions'], 0)
        self.assertEqual(self.g('A')[MAPPING][HOST]['restored'], 'p')
        self.assertEqual(self.g('B')[PROJECTS]['restored']['name'], 'Recreated')
        self.assertNotIn('a', self.g('A')[PROJECTS])

    def test_renderer_cloud_conversion_and_invalid_order(self):
        self.sync()
        self.g('C')[PROJECTS]['a']['chatGptBacking'] = {'secret': 'private'}
        self.profiles['D'] = {'globals': {}, 'atoms': {}}
        self.g('C')[CHAT_ORDER]['b'] = {'threadIds': ['t3'], 'future': {'opaque': 1}}
        self.sync()
        self.assertNotIn('a', self.g('D')[PROJECTS])
        self.assertNotIn('b', self.g('D').get(CHAT_ORDER, {}))
        self.assertEqual(self.g('C')[PROJECTS]['a']['chatGptBacking'], {'secret': 'private'})
        self.assertNotIn('private', repr(self.base))
        self.g('C')[ORDER] = [{'unsupported': True}]
        before = deepcopy(self.profiles)
        with self.assertRaises(ValueError):
            self.sync()
        self.assertEqual(before, self.profiles)

    def test_bootstrap_rename_not_verified_by_sql_or_ambiguous_alias(self):
        self.g('A')[PROJECTS]['a']['name'] = 'Uncommitted rename'
        self.g('A')[MAPPING][HOST]['second-alias'] = 'q'
        self.assertEqual(self.sync()['projects'], 0)
        self.assertEqual(self.g('A')[PROJECTS]['a']['name'], 'Uncommitted rename')
        self.assertNotIn(PROJECTS, self.g('B'))


if __name__ == '__main__':
    unittest.main()
