"""The migrated desktop layout has native shared projects and ephemeral input recall."""
import copy
import os
import stat
import time
import uuid

import native_layout
import recall
import sync


LAYOUT = 'shared-v2'
KEY = 'prompt-history'


def context(raw, policy):
    state = sync.read_json(raw)
    layout = sync.inspect_layout(state)
    sync.need(layout['kind'] == LAYOUT, 'native-layout-changed-deferred')
    owners = set(layout['owners'])
    if policy['scope'] == 'selected':
        owners.intersection_update(policy['owners'])
    return state, layout, owners


def selected_history(state):
    atoms = state.get(sync.ATOMS)
    sync.need(isinstance(atoms, dict), 'unsupported-live-atom-layout')
    return [copy.deepcopy(atoms[KEY])] if KEY in atoms else []


def scope_values(state, keys):
    hist = state[sync.ATOMS].get(KEY,{})
    sync.need(isinstance(hist,dict),'restore-point-diverged')
    return {k:[copy.deepcopy(hist[k])] if k in hist else [] for k in keys}


def restore_scopes(state, receipt):
    hist=state[sync.ATOMS].setdefault(KEY,{})
    for k,value in receipt['prior'].items():
        if value:
            hist[k]=value[0]
        else:
            hist.pop(k,None)
    if receipt['created_atom'] and not hist:
        state[sync.ATOMS].pop(KEY,None)


def paths(store):
    return store.baseline.parent/'recall-v2.json', store.baseline.parent/'capture-status.json'


def consent_unchanged(store, token, policy):
    return sync.read_regular(store.enrollment, True) == token and store.policy() == policy


def check_envelope(store, value, policy):
    sync.need(value.get('layout') == LAYOUT and value.get('version') == 1
              and value.get('policy') == sync.fingerprint(sync.packed(policy))
              and value.get('generation') == store._location()[2], 'recall-scope-mismatch')


def envelope(store, ledger, policy):
    return {'version':1, 'layout':LAYOUT, 'generation':store._location()[2],
            'policy':sync.fingerprint(sync.packed(policy)), 'ledger':ledger}


def retained_owners(ledger, owners, policy):
    if policy['scope']=='selected':
        return set(policy['owners'])
    # The envelope already proves this unchanged all-profile consent.
    # Missing native identities remain private and do not participate in merge.
    cached=set()
    for key in ('histories','observed','native_views'):
        cached.update(ledger.get(key,{}))
    return set(owners)|cached


def load_ledger(store, policy, raw, owners, allowed, fresh=False):
    file, _ = paths(store)
    old = None if fresh else store.load(file, None)
    if old is not None:
        check_envelope(store, old, policy)
        return old['ledger'], old
    # Earlier verified writes contain pre-change cache fragments. This is a
    # bounded recovery of those private fragments, not a transcript scan.
    receipts = []
    if not fresh:
        for path in sorted(store.backups.glob('*.json'), reverse=True)[:sync.MAX_BACKUPS]:
            item = store.load(path, None)
            if item.get('layout') is not None:
                continue
            try:
                store._guard_generation(item, store._location()[2])
                sync.guard_scope(item, policy)
            except sync.Refusal:
                continue
            receipts.append(item)
    ledger = recall.seed(None, receipts, owners, allowed)
    return ledger, old


def report(base, owners, changed=False):
    return {'profiles':len(owners),'changed_fields':int(changed),
            'projects':{'native_shared':True},
            'history':sync.numbers_only(base)}


def capture(store):
    token = sync.read_regular(store.enrollment, True)
    policy = store.policy()
    if not sync.history_enabled(policy):
        return {'state':'capture-disabled'}
    raw = sync.read_regular(store.live)
    layout = sync.inspect_layout(sync.read_json(raw))
    if layout['kind'] != LAYOUT:
        return {'state':'capture-not-required'}
    sync.need(not os.path.lexists(store.pending), 'pending-transaction')
    state, layout, owners = context(raw, policy)
    active = layout['active']
    if active not in owners:
        return {'state':'capture-no-enrolled-active-profile'}
    inventory = sync.local_inventory(store.codex, include_projects=False)
    allowed = inventory['history_threads']
    ledger, old = load_ledger(store, policy, raw, owners, allowed)
    updated, counts = recall.capture(state, ledger, owners, active, allowed,
                                     retained_owners=retained_owners(ledger,owners,policy))
    counts.update(retained_profiles=len(updated['histories']),
                  retained_scopes=sum(len(v) for v in updated['histories'].values()),
                  retained_entries=sum(len(rows) for v in updated['histories'].values() for rows in v.values()))
    wrapped = envelope(store, updated, policy)
    file, status_file = paths(store)
    # A native writer can change identity while we read the index. Never
    # attribute that newer state to the identity we originally observed.
    sync.need(sync.read_regular(store.live) == raw and consent_unchanged(store,token,policy),
              'capture-source-changed')
    if wrapped != old:
        sync.replace(file, sync.as_json(wrapped))
    prior_status = store.load(status_file, {})
    if wrapped != old or prior_status.get('state') != 'captured':
        sync.replace(status_file, sync.packed({'state':'captured','time':int(time.time()),
                                               'counts':sync.numbers_only(counts)}))
    return {'state':'captured' if wrapped != old else 'capture-unchanged',
            'counts':sync.numbers_only(counts)}


def validate_tx(store, tx, policy):
    sync.need(tx.get('layout') == LAYOUT and tx.get('generation') == store._location()[2]
              and tx.get('policy') == sync.fingerprint(sync.packed(policy)), 'incompatible-recovery-layout')
    sync.need(sync.history_enabled(policy) and
              (policy['scope']=='all' or tx.get('owner') in policy['owners']), 'recovery-outside-enrollment')
    keys=tx.get('scope_keys')
    sync.need(isinstance(keys,list) and bool(keys) and len(keys)==len(set(keys))
              and all(isinstance(k,str) and recall.UUID.fullmatch(k) for k in keys)
              and set(tx.get('prior',{}))==set(keys) and set(tx.get('next',{}))==set(keys)
              and type(tx.get('created_atom')) is bool,'invalid-recall-recovery-scope')
    for side in ('old_ledger','new_ledger'):
        if tx.get(side) is not None:
            check_envelope(store,tx[side],policy)


def finish(store, tx, applied):
    if applied:
        validate_tx(store,tx,store.policy())
        backup = store.backups/(tx['id']+'.json')
        if not backup.exists():
            sync.replace(backup,sync.as_json(tx))
        file,_ = paths(store)
        sync.replace(file,sync.as_json(tx['new_ledger']))
        store.mark('restored-modern-history' if tx.get('rollback') else 'synced-modern-history',tx['counts'])
    store.pending.unlink()
    sync.directory_sync(store.data)
    if not applied:
        return
    for f in sorted(store.backups.glob('*.json'), reverse=True)[sync.MAX_BACKUPS:]:
        sync.check_file(f,True)
        f.unlink()


def recover(store, raw, policy):
    if not os.path.lexists(store.pending):
        return
    tx = store.load(store.pending,None)
    validate_tx(store,tx,policy)
    state, layout, _ = context(raw,policy)
    sync.need(layout['active']==tx['owner'],'pending-owner-changed-needs-review')
    current=scope_values(state,tx['scope_keys'])
    if sync.fingerprint(raw)==tx['after'] or current==tx['next']:
        finish(store,tx,True)
    elif sync.fingerprint(raw)==tx['before'] or current==tx['prior']:
        finish(store,tx,False)
    else:
        raise sync.Refusal('interrupted-transaction-needs-review')


def update(store, action, raw, policy, fresh=False):
    token=sync.read_regular(store.enrollment,True) if os.path.lexists(store.enrollment) else None
    if action != 'plan':
        sync.need(store.is_offline(),'app-running-deferred')
        recover(store,raw,policy)
        raw=sync.read_regular(store.live)
    else:
        sync.need(not os.path.lexists(store.pending),'pending-transaction')
    state, layout, owners=context(raw,policy)
    if action == 'restore':
        points=sorted(store.backups.glob('*.json'),reverse=True)
        sync.need(bool(points),'no-recovery-point')
        tx=store.load(points[0],None)
        validate_tx(store,tx,policy)
        inv=sync.local_inventory(store.codex,include_projects=False)
        sync.need(set(tx['scope_keys']) <= inv['history_threads'], 'recovery-thread-no-longer-local')
        sync.need(layout['active']==tx['owner'] and scope_values(state,tx['scope_keys'])==tx['next'],
                  'restore-point-diverged')
        prior=selected_history(state)
        restore_scopes(state,tx)
        old_ledger=store.load(paths(store)[0],None)
        new_ledger=tx['old_ledger']
        sync.need(new_ledger is not None,'recall-restore-baseline-missing')
        counts=report({},owners,prior!=tx['prior'])
    elif not sync.history_enabled(policy):
        counts=report({'enabled':False},owners)
        if action != 'plan':
            store.mark('native-project-sharing',counts)
        return {'state':'preview' if action=='plan' else 'native-project-sharing',
                'counts':counts,'baseline_would_change':False}
    elif layout['active'] not in owners:
        return {'state':'no-enrolled-active-profile','counts':report({},owners),'baseline_would_change':False}
    else:
        inv=sync.local_inventory(store.codex,include_projects=False)
        allowed=inv['history_threads']
        ledger,old_ledger=load_ledger(store,policy,raw,owners,allowed,fresh)
        # Seeded fragments are themselves a reversible starting state even when
        # the format-specific ledger hasn't yet been persisted.
        if old_ledger is None:
            old_ledger=envelope(store,copy.deepcopy(ledger),policy)
        prior=selected_history(state)
        ledger,history_counts=recall.merge(state,ledger,owners,layout['active'],allowed,
                                           retained_owners=retained_owners(ledger,owners,policy))
        new_ledger=envelope(store,ledger,policy)
        counts=report(history_counts,owners,prior!=selected_history(state))
    next_value=selected_history(state)
    if action=='plan':
        return {'state':'preview','native_layout':LAYOUT,'counts':counts,
                'baseline_would_change':new_ledger!=old_ledger}
    if next_value==prior:
        sync.need(sync.read_regular(store.live)==raw
                  and consent_unchanged(store,token,policy) and store.is_offline(),'source-changed-deferred')
        if store.load(paths(store)[0],None)!=new_ledger:
            sync.replace(paths(store)[0],sync.as_json(new_ledger))
        store.mark('modern-history-current',counts)
        return {'state':'modern-history-current','counts':counts}
    output=sync.as_json(state)
    before_hist=prior[0] if prior else {}
    after_hist=next_value[0] if next_value else {}
    changed=sorted(k for k in set(before_hist)|set(after_hist)
                   if before_hist.get(k,sync.MISSING)!=after_hist.get(k,sync.MISSING))
    receipt={'layout':LAYOUT,'id':f'{time.time_ns():020}-{uuid.uuid4().hex[:8]}',
             'generation':store._location()[2],'policy':sync.fingerprint(sync.packed(policy)),
             'owner':layout['active'],'scope_keys':changed,'created_atom':not prior,
             'prior':{k:[before_hist[k]] if k in before_hist else [] for k in changed},
             'next':{k:[after_hist[k]] if k in after_hist else [] for k in changed},
             'before':sync.fingerprint(raw),'after':sync.fingerprint(output),
             'old_ledger':old_ledger,'new_ledger':new_ledger,'counts':counts,'rollback':action=='restore'}
    sync.check_native_metadata(store.live)
    with sync.staged_file(store.live,output,stat.S_IMODE(sync.check_file(store.live).st_mode)) as tmp:
        sync.need(tmp.stat().st_gid==store.live.stat().st_gid,'nonstandard-native-file-group')
        sync.need(sync.read_regular(tmp)==output,'staging-verification-failed')
        sync.need(store.is_offline(),'app-running-deferred')
        sync.replace(store.pending,sync.as_json(receipt))
        try:
            stable=inv is None or sync.local_inventory(store.codex,include_projects=False)==inv
            safe=stable and sync.read_regular(store.live)==raw and consent_unchanged(store,token,policy) and store.is_offline()
        except Exception:
            finish(store,receipt,False)
            raise
        if not safe:
            finish(store,receipt,False)
            raise sync.Refusal('source-changed-deferred')
        os.replace(tmp,store.live)
        sync.directory_sync(store.codex)
        sync.need(sync.read_regular(store.live)==output,'post-commit-recovery-needed')
    finish(store,receipt,True)
    return {'state':'restored-modern-history' if action=='restore' else 'synced-modern-history','counts':counts}
