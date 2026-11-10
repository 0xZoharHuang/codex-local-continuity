"""Three-way sync of expressly enrolled, local native sidebar views. Pure Python."""
from copy import deepcopy
from pathlib import PurePosixPath


PROJECTS = 'local-projects'
MAPPING = 'app-server-project-id-by-legacy-project-id-by-host'
ASSIGN = 'thread-project-assignments'
UNASSIGN = 'projectless-thread-ids'
HOSTS = 'thread-project-membership-host-ids'
ORDER = 'project-order'
CHAT_ORDER = 'sidebar-project-thread-orders'
FIELDS = {PROJECTS: dict, MAPPING: dict, ASSIGN: dict, UNASSIGN: list, HOSTS: dict,
          ORDER: list, CHAT_ORDER: dict}
PLAIN = {'id', 'name', 'rootPaths', 'createdAt', 'updatedAt'}
NONE = 'ungrouped'


def require(condition, msg):
    if not condition:
        raise ValueError(msg)


def distinct(items):
    return list(dict.fromkeys(items))


def replace_order(existing, known, new):
    """Replace only shareable slots. Opaque entries keep their relative order."""
    remaining = iter(new)
    result = []
    for item in existing:
        if item in known:
            item = next(remaining, None)
        if item is not None:
            result.append(item)
    return result + list(remaining)


def project_data(obj, alias):
    if (not isinstance(obj, dict) or set(obj) - PLAIN or obj.get('id') != alias
            or not isinstance(obj.get('name'), str) or not obj['name'].strip()):
        return None
    roots = obj.get('rootPaths')
    if (not isinstance(roots, list) or not roots or not all(isinstance(p, str) and
            PurePosixPath(p).is_absolute() and '\0' not in p and p != '/' for p in roots)):
        return None
    if not all(type(obj.get(k)) is int for k in ('createdAt', 'updatedAt')):
        return None
    return {'name': obj['name'], 'roots': roots[:]}


def capture(g, inv, base, seeds, previous_fields, last_aliases=None):
    """Collect native→legacy aliases and comparable observations, with no guessing by cwd."""
    for field, typ in FIELDS.items():
        require(isinstance(g.get(field, typ()), typ), 'Unexpected project field type: ' + field)
    # Existing materialized keys disappearing together can be an unloaded profile.
    # A new authenticated empty profile has no previous materialization.
    missing = {f for f in previous_fields if f not in g}
    if missing:
        return None
    host = inv['host']
    ids = g.get(MAPPING, {}).get(host, {})
    require(isinstance(ids, dict) and all(isinstance(k, str) and isinstance(v, str)
            for k, v in ids.items()), 'Unexpected host mapping type')
    store = g.get(PROJECTS, {})
    native = inv['projects']
    for field in (UNASSIGN, ORDER):
        items = g.get(field, [])
        require(all(isinstance(x, str) for x in items) and len(items) == len(set(items)),
                'Unexpected native sidebar sequence')
    if last_aliases:
        # An empty but structurally valid transient projection has the same keys
        # as a loaded one. Retain this owner's last view and abstain from merging.
        if not store:
            return None
        if any(alias not in store and pid in native for pid, alias in last_aliases.items()):
            return None
    reverse_ids = {}
    for a, p in ids.items():
        reverse_ids.setdefault(p, []).append(a)
    aliases, blocked, protected, data = {}, set(), set(), {}
    blocked_aliases = set()
    for alias, obj in store.items():
        pid = ids.get(alias) or inv.get('aliases', {}).get(alias) or (alias if alias in native else None)
        p = project_data(obj, alias)
        canonical_alias = inv.get('aliases', {}).get(alias)
        if (not p or pid not in native or canonical_alias not in (None, pid)
                or len(reverse_ids.get(pid, [])) > 1):
            blocked_aliases.add(alias)
            if pid:
                blocked.add(pid)
            continue
        if pid in aliases:
            blocked.add(pid)
            continue
        sql = {'name': native[pid]['name'], 'roots': native[pid]['roots']}
        if p != sql and ('p/' + pid) not in base:
            blocked.add(pid)
            blocked_aliases.add(alias)
            continue
        aliases[pid] = alias
        data['p/' + pid] = p
        # Initialize with an authenticated, native-matching definition only.
        if p == sql:
            if pid not in seeds:
                seeds[pid] = {'alias': alias, 'createdAt': obj['createdAt'], 'updatedAt': obj['updatedAt']}
            else:
                seeds[pid]['updatedAt'] = max(seeds[pid]['updatedAt'], obj['updatedAt'])
    for pid in blocked:
        data.pop('p/' + pid, None)
        aliases.pop(pid, None)
    reverse = {a: p for p, a in aliases.items()}
    hosts = g.get(HOSTS, {})
    # A per-thread host hint also governs the independent chat-order lists.
    # It may survive even when this profile has no explicit assignment.
    protected.update(tid for tid, host_hint in hosts.items() if host_hint not in (None, 'local'))
    for tid, assignment in g.get(ASSIGN, {}).items():
        if (tid not in inv['threads'] or hosts.get(tid) not in (None, 'local')
                or not isinstance(assignment, dict) or
                set(assignment) != {'projectId', 'projectKind'} or assignment['projectKind'] != 'local'
                or assignment['projectId'] not in reverse):
            protected.add(tid)
            continue
        data['t/' + tid] = reverse[assignment['projectId']]
    for tid in g.get(UNASSIGN, []):
        if tid in inv['threads'] and tid not in protected and hosts.get(tid) in (None, 'local'):
            require('t/' + tid not in data, 'Contradictory explicit local thread state')
            data['t/' + tid] = NONE
    if ORDER in g:
        data['order'] = distinct(reverse[a] for a in g[ORDER] if a in reverse)
    for alias, entry in g.get(CHAT_ORDER, {}).items():
        if (alias not in reverse or not isinstance(entry, dict) or set(entry) != {'threadIds'}
                or not isinstance(entry.get('threadIds'), list)):
            continue
        order = entry['threadIds']
        require(all(isinstance(t, str) for t in order) and len(order) == len(set(order)),
                'Unexpected native chat order')
        data['s/' + reverse[alias]] = [t for t in order if t in inv['threads'] and t not in protected]
    return {'data': data, 'aliases': aliases, 'blocked': blocked, 'blocked_aliases': blocked_aliases,
            'protected': protected}


def choose(key, now, prior, canonical, old_conflict):
    """Record incompatible edits with participants; equal later edits resolve them."""
    changes = {owner: value for owner, value in now.items()
               if (owner not in prior and value != canonical) or
                  (owner in prior and value != prior[owner])}
    if key == 'order' or key.startswith('s/'):
        # A bare empty vector is indivisible from renderer initialization. Preserve
        # a historical non-empty order until an observable reorder or deletion.
        changes = {o: v for o, v in changes.items() if v}
    if old_conflict:
        contenders = dict(old_conflict)
        for owner in contenders:
            if owner in now:
                contenders[owner] = now[owner]
        contenders.update(changes)
    else:
        contenders = changes
        # A newly enrolled owner's existing, differing explicit choice is not a
        # more recent edit than an already materialized choice on another owner.
        if canonical is not None and any(o not in prior for o in changes):
            contenders.update({o: v for o, v in now.items() if o in prior and v == canonical})
    vals = list(contenders.values())
    if not vals:
        return canonical, {}
    if all(v == vals[0] for v in vals):
        return vals[0], {}
    # Two clients may append different new objects to precisely the same old order.
    # General concurrent reorder is left intact until the two views converge.
    if (key == 'order' or key.startswith('s/')) and isinstance(canonical, list):
        if all(isinstance(v, list) and v[:len(canonical)] == canonical for v in vals):
            return distinct(canonical + [x for o in sorted(contenders) for x in contenders[o]]), {}
    return canonical, contenders


def record_field(g, key, value):
    if key in g or value:
        g[key] = value


def apply(g, observation, canonical, conflicts, removed, inv, seeds, last_aliases):
    s = observation
    catalog = g.get(PROJECTS, {}).copy()
    host = inv['host']
    maps = g.get(MAPPING, {}).copy()
    mapping = maps.get(host, {}).copy()
    aliases = s['aliases'].copy()
    # Only remove entries with confirmed native deletion plus explicit local removal.
    removed_aliases = {alias: pid for pid, alias in last_aliases.items() if pid in removed
                       and mapping.get(alias, pid) == pid and
                       (alias not in catalog or project_data(catalog[alias], alias))}
    for alias, pid in list(mapping.items()):
        if pid in removed:
            obj = catalog.get(alias)
            if obj is not None and not project_data(obj, alias):
                continue
            catalog.pop(alias, None)
            del mapping[alias]
            removed_aliases[alias] = pid
    for key, val in canonical.items():
        if not key.startswith('p/'):
            continue
        pid = key[2:]
        if pid not in inv['projects'] or pid in s['blocked'] or key in conflicts:
            continue
        orphaned_aliases = [a for a, target in mapping.items() if target == pid]
        if len(orphaned_aliases) > 1:
            continue
        alias = aliases.get(pid) or (orphaned_aliases[0] if orphaned_aliases else seeds[pid]['alias'])
        if (alias in s['blocked_aliases'] or mapping.get(alias, pid) != pid
                or inv.get('aliases', {}).get(alias, pid) != pid
                or (alias in catalog and pid not in aliases)):
            continue
        if pid not in aliases:
            # Never silently fuse a different project merely because a root/name matches.
            if any(isinstance(p, dict) and
                   (p.get('name') == val['name'] or set(p.get('rootPaths') or []) & set(val['roots']))
                   for p in catalog.values()):
                continue
        aliases[pid] = alias
        mapping[alias] = pid
        obj = catalog.get(alias, {'id': alias, 'createdAt': seeds[pid]['createdAt'],
                                  'updatedAt': seeds[pid]['updatedAt']})
        if obj.get('name') != val['name'] or obj.get('rootPaths') != val['roots']:
            catalog[alias] = dict(obj, name=val['name'], rootPaths=val['roots'][:],
                                  updatedAt=max(obj['updatedAt'], seeds[pid]['updatedAt']))
    if host in maps or mapping:
        maps[host] = mapping
    record_field(g, PROJECTS, catalog)
    record_field(g, MAPPING, maps)
    ass = g.get(ASSIGN, {}).copy()
    un = dict.fromkeys(g.get(UNASSIGN, []))
    hints = g.get(HOSTS, {}).copy()
    for tid, val in list(ass.items()):
        if (isinstance(val, dict) and set(val) == {'projectKind', 'projectId'}
                and val['projectKind'] == 'local' and val['projectId'] in removed_aliases
                and tid in inv['threads'] and hints.get(tid) in (None, 'local')):
            del ass[tid]
            hints.pop(tid, None)
    for key, val in canonical.items():
        if not key.startswith('t/') or key in conflicts:
            continue
        tid = key[2:]
        if (tid not in inv['threads'] or tid in s['protected'] or
                hints.get(tid) not in (None, 'local')):
            continue
        if val == NONE:
            ass.pop(tid, None)
            un[tid] = None
        elif val in aliases and 'p/' + val not in conflicts:
            ass[tid] = {'projectKind': 'local', 'projectId': aliases[val]}
            un.pop(tid, None)
        # Host hints supplement existing membership records; no new grant/hint is needed.
    record_field(g, ASSIGN, ass)
    record_field(g, UNASSIGN, list(un))
    record_field(g, HOSTS, hints)
    old_order = g.get(ORDER, [])
    kept = [a for a in old_order if a not in removed_aliases]
    if 'order' in canonical and 'order' not in conflicts:
        desired = [aliases[p] for p in canonical['order'] if p in aliases]
        kept = replace_order(kept, set(aliases.values()), desired)
    record_field(g, ORDER, kept)
    orders = {a: v for a, v in g.get(CHAT_ORDER, {}).items() if a not in removed_aliases}
    for key, seq in canonical.items():
        if not key.startswith('s/') or key in conflicts or key[2:] not in aliases:
            continue
        alias = aliases[key[2:]]
        original = orders.get(alias, {'threadIds': []})
        if (not isinstance(original, dict) or set(original) != {'threadIds'}
                or not isinstance(original.get('threadIds'), list)):
            continue
        eligible = inv['threads'] - s['protected']
        sequence = [t for t in seq if t in eligible]
        out = replace_order(original['threadIds'], eligible, sequence)
        if out or alias in orders:
            orders[alias] = dict(original, threadIds=out)
    record_field(g, CHAT_ORDER, orders)


def sync_projects(profiles, previous, inventory):
    """Project only the local sidebar metadata for the supplied enrolled owners."""
    require(isinstance(inventory['threads'], set) and isinstance(inventory['projects'], dict)
            and inventory['host'].startswith('local:/'), 'Incomplete native project inventory')
    # Non-empty backend metadata can indicate a new cloud-backed association.
    # Parents may already filter these rows; never fan out a prior local snapshot.
    inventory = dict(inventory, projects={k: p for k, p in inventory['projects'].items()
                     if p.get('metadata', {}) == {}})
    # A renderer can already hold backing/extension metadata that SQLite hasn't
    # recorded yet. Hide that ID from all cross-owner materialization this pass.
    quarantined = set()
    for profile in profiles.values():
        g = profile['globals']
        catalog = g.get(PROJECTS, {})
        maps = g.get(MAPPING, {})
        require(isinstance(catalog, dict) and isinstance(maps, dict), 'Unexpected catalog schema')
        local_map = maps.get(inventory['host'], {})
        require(isinstance(local_map, dict), 'Unexpected host mapping type')
        for alias, obj in catalog.items():
            pid = local_map.get(alias) or inventory.get('aliases', {}).get(alias) or alias
            if pid in inventory['projects'] and not project_data(obj, alias):
                quarantined.add(pid)
    inventory['projects'] = {k: p for k, p in inventory['projects'].items() if k not in quarantined}
    previous = previous or {'version': 1, 'canonical': {}, 'owners': {}, 'conflicts': {},
                            'seeds': {}, 'removed': []}
    require(previous['version'] == 1, 'Unknown project baseline version')
    canonical = deepcopy(previous['canonical'])
    seeds = deepcopy(previous['seeds'])
    last = previous['owners']
    views = {}
    for owner, profile in profiles.items():
        prev = last.get(owner, {})
        obs = capture(profile['globals'], inventory, canonical, seeds, prev.get('fields', []), prev.get('aliases'))
        if obs is not None:
            views[owner] = obs
    removed = set(previous.get('removed', []))
    all_ids = inventory.get('all_project_ids')
    if all_ids is not None:
        require(set(inventory['projects']) <= set(all_ids), 'Contradictory native project inventory')
        for pid in set(removed) & set(inventory['projects']):
            native = inventory['projects'][pid]
            restored = {'name': native['name'], 'roots': native['roots']}
            for owner, view in views.items():
                if view['data'].get('p/' + pid) == restored:
                    removed.remove(pid)
                    alias = view['aliases'][pid]
                    obj = profiles[owner]['globals'][PROJECTS][alias]
                    seeds[pid] = {'alias': alias, 'createdAt': obj['createdAt'], 'updatedAt': obj['updatedAt']}
                    break
        for key in list(canonical):
            if not key.startswith('p/') or key[2:] in all_ids:
                continue
            pid = key[2:]
            # No per-owner removal can be inferred on an unmaterialized/new profile.
            for owner in views:
                before = last.get(owner, {}).get('aliases', {}).get(pid)
                g = profiles[owner]['globals']
                if before and PROJECTS in g and before not in g[PROJECTS]:
                    removed.add(pid)
                    break
    keys = set(canonical).union(*(v['data'] for v in views.values()))
    conflicts = deepcopy(previous.get('conflicts', {}))
    for key in sorted(keys):
        now = {o: s['data'][key] for o, s in views.items() if key in s['data']}
        before = {o: v['data'][key] for o, v in last.items() if key in v['data']}
        val, conflict = choose(key, now, before, canonical.get(key), conflicts.get(key))
        if conflict:
            conflicts[key] = conflict
        else:
            conflicts.pop(key, None)
        if val is not None:
            canonical[key] = val
    # Accept verified project writes only. A stale renderer cannot undo a newer SQL rename.
    for key, val in list(canonical.items()):
        if not key.startswith('p/') or key[2:] not in inventory['projects']:
            continue
        pid = key[2:]
        native = inventory['projects'][pid]
        sql = {'name': native['name'], 'roots': native['roots']}
        old = previous['canonical'].get(key)
        observed = {o: v['data'][key] for o, v in views.items() if key in v['data']}
        if old is not None and observed and sql != old:
            # SQL can lead or lag the renderer. A native-validated change of name
            # and a simultaneous change of roots are independent fields.
            combined = {}
            for field in ('name', 'roots'):
                values = [p[field] for p in observed.values() if p[field] != old[field]]
                if values and any(v != values[0] for v in values):
                    break
                combined[field] = values[0] if values else old[field]
            if combined == sql:
                canonical[key] = sql
                conflicts.pop(key, None)
                continue
        if val != sql:
            if key in previous['canonical']:
                canonical[key] = previous['canonical'][key]
            else:
                del canonical[key]
            if any(p != sql for p in observed.values()):
                conflicts.setdefault(key, {o: p for o, p in observed.items() if p == val})
    # Never honor a stale tombstone against a re-created, retained or now-opaque
    # backend record. No complete native-ID inventory means no deletions.
    deletions = removed - set(all_ids) if all_ids is not None else set()
    for pid in deletions:
        for key in list(canonical):
            if key in ('p/' + pid, 's/' + pid) or (key.startswith('t/') and canonical[key] == pid):
                canonical.pop(key, None)
                conflicts.pop(key, None)
        if 'order' in canonical:
            canonical['order'] = [x for x in canonical['order'] if x != pid]
    for key, seq in list(canonical.items()):
        if key.startswith('s/'):
            pid = key[2:]
            canonical[key] = [t for t in seq if 't/' + t in conflicts
                              or canonical.get('t/' + t, pid) == pid]
    next_owners = deepcopy(last)
    staged = {}
    for owner, observation in views.items():
        g = profiles[owner]['globals'].copy()
        apply(g, observation, canonical, conflicts, deletions, inventory, seeds, last.get(owner, {}).get('aliases', {}))
        snap = capture(g, inventory, canonical, seeds, ())
        next_owners[owner] = {'data': snap['data'], 'aliases': snap['aliases'],
                              'fields': [f for f in FIELDS if f in g]}
        staged[owner] = g
    for owner, g in staged.items():
        profiles[owner]['globals'].update({k: g[k] for k in FIELDS if k in g})
    baseline = {'version': 1, 'canonical': canonical, 'owners': next_owners, 'conflicts': conflicts,
                'seeds': seeds, 'removed': sorted(removed)}
    counts = {'projects': sum(k.startswith('p/') for k in canonical),
              'memberships': sum(k.startswith('t/') for k in canonical),
              'conflicts': len(conflicts), 'partial_profiles': len(profiles) - len(views),
              'confirmed_deletions': len(removed)}
    return baseline, counts
