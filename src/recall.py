"""Private local recall cache for native layouts that clear recall on account changes.

Capture changes only this helper ledger. Merge changes only the active native
prompt-history atom. Persistence, consent, native layout and transaction checks
belong to the caller. An empty native recall window is treated as an account
reset; a deliberate empty clear is indistinguishable and is also recoverable.
"""
from collections import Counter, defaultdict
from copy import deepcopy

from history import (ATOM, CAP, GLOBAL_KEYS, SCOPE_CAP, UUID, _bind, _digest,
                     _shareable, _merge_order, sync_history)


ATOMS = 'electron-persisted-atom-state'


def _allowed(threads):
    return {t for t in threads if isinstance(t, str) and UUID.fullmatch(t)}


def _prepare(ledger, owners, retained_owners=None):
    # The transaction owner validates the unchanged policy and generation before
    # permitting historical identities to remain in this private envelope.
    # Only currently verified owners participate in capture or merge below.
    permitted = set(owners) if retained_owners is None else set(retained_owners)
    if not set(owners) <= permitted:
        raise ValueError('recall-owner-outside-retention-scope')
    if ledger is None:
        engine, _ = sync_history({}, None, {'threads': set()})
        result = {'version': 1, 'engine': engine, 'histories': {}, 'observed': {},
                  'native_views': {}, 'capture_sequence': 0}
    else:
        if not isinstance(ledger, dict) or ledger.get('version') != 1:
            raise ValueError('unsupported-recall-ledger')
        result = deepcopy(ledger)
    if set(result) != {'version', 'engine', 'histories', 'observed', 'native_views', 'capture_sequence'}:
        raise ValueError('unsupported-recall-ledger')
    for field in ('histories', 'observed', 'native_views'):
        if not isinstance(result[field], dict) or not set(result[field]) <= permitted:
            raise ValueError('recall-ledger-outside-enrollment')
    if type(result['capture_sequence']) is not int or result['capture_sequence'] < 0:
        raise ValueError('invalid-recall-sequence')
    key = bytes.fromhex(result['engine']['key'])
    for histories in result['histories'].values():
        if not isinstance(histories, dict) or len(histories) > SCOPE_CAP:
            raise ValueError('invalid-recall-cache')
        for scope, records in histories.items():
            if (not isinstance(scope, str) or not UUID.fullmatch(scope) or not isinstance(records, list)
                    or len(records) > CAP or any(not isinstance(r, dict) or set(r) != {'plain'}
                    or _shareable(r) is None for r in records)):
                raise ValueError('invalid-recall-cache')
    return result, key


def _trim(scopes):
    return dict(list(scopes.items())[-SCOPE_CAP:])


def _report():
    return Counter(captured_scopes=0, captured_entries=0, observed_sends=0,
                   skipped_scopes=0, opaque_records=0, restored_scopes=0,
                   restored_entries=0, native_scopes_changed=0, seeded_scopes=0)


def _observe(state, result, key, active, allowed, report):
    atoms = state.get(ATOMS)
    hist = atoms.get(ATOM, {}) if isinstance(atoms, dict) else None
    if not isinstance(hist, dict):
        report['skipped_scopes'] += 1
        return
    private = result['histories'].setdefault(active, {})
    seen = result['observed'].setdefault(active, {})
    # A witnessed empty/absent native window is a reset boundary. Keep its
    # cached safe occurrences for recall, but end the old observation sequence
    # so an equal prompt submitted after the reset remains a separate send.
    for scope in seen:
        if scope in allowed and (scope not in hist or hist[scope] == []):
            seen[scope] = []

    def issue():
        result['capture_sequence'] += 1
        return result['capture_sequence']

    eligible = [s for s in hist if s in allowed][-SCOPE_CAP:]
    for scope in eligible:
        rows = hist[scope]
        if not isinstance(rows, list) or len(rows) > CAP:
            report['skipped_scopes'] += 1
            continue
        if not rows:
            continue
        fingerprints = [_digest(key, ['record', r]) for r in rows]
        prior = seen.get(scope, [])
        ids, fresh, _ = _bind(prior, fingerprints, issue, scope in seen)
        seen[scope] = [[fp, eid] for fp, eid in zip(fingerprints, ids)]
        report['observed_sends'] += len(fresh)
        safe = [{'plain': body} for r in rows if (body := _shareable(r)) is not None]
        report['opaque_records'] += len(rows) - len(safe)
        if not safe:
            continue
        cached = private.get(scope)
        if cached is None:
            combined = safe
        elif fresh:
            added = [{'plain': body} for r, eid in zip(rows, ids)
                     if eid in fresh and (body := _shareable(r)) is not None]
            combined = (cached + added)[-CAP:]
        elif prior and [r[0] for r in prior] == fingerprints:
            combined = cached
        else:
            combined = _restore_rows(safe, cached)
        if cached != combined:
            report['captured_scopes'] += 1
            report['captured_entries'] += len(combined)
        private.pop(scope, None)
        private[scope] = combined
    result['histories'][active] = _trim(private)
    result['observed'][active] = _trim(seen)


def _restore_rows(raw, cached):
    """Bounded one-to-one occurrence union that retains every native original."""
    available = defaultdict(list)
    for i, row in enumerate(cached):
        available[_shareable(row)].append(i)
    matched = set()
    original_ids, values = [], {}
    for i, row in enumerate(raw):
        body = _shareable(row)
        if body is not None and available[body]:
            cid = available[body].pop(0)
            eid = ('cache', cid)
            matched.add(cid)
        else:
            eid = ('raw', i)
        original_ids.append(eid)
        values[eid] = row
    room = CAP - len(raw)
    missing = [i for i in range(len(cached)) if i not in matched]
    accepted = missing[-room:] if room else []
    for i in accepted:
        values[('cache', i)] = deepcopy(cached[i])
    incoming = [('cache', i) for i in range(len(cached)) if i in matched or i in accepted]
    return [values[eid] for eid in _merge_order(original_ids, incoming)]


def capture(state, ledger, owners, active, allowed_threads, *, retained_owners=None):
    """Capture bounded shareable text into an isolated private ledger; leave state intact."""
    result, key = _prepare(ledger, owners, retained_owners)
    report = _report()
    if active in owners:
        _observe(state, result, key, active, _allowed(allowed_threads), report)
    return result, dict(report)


def merge(state, ledger, owners, active, allowed_threads, *, retained_owners=None):
    """Restore/merge recall using existing occurrence-aware history logic, then cache it."""
    result, report_dict = capture(state, ledger, owners, active, allowed_threads,
                                  retained_owners=retained_owners)
    report = Counter(report_dict)
    atoms = state.get(ATOMS)
    if active not in owners or not isinstance(atoms, dict):
        return result, dict(report)
    original = atoms.get(ATOM, {})
    if not isinstance(original, dict):
        report['skipped_scopes'] += 1
        return result, dict(report)
    allowed = _allowed(allowed_threads)
    key = bytes.fromhex(result['engine']['key'])
    working = deepcopy(original)
    # Fill only safe cache occurrences absent from the active raw list. Existing
    # rich and opaque native records keep their exact representations and order.
    restored = {}
    for scope, records in result['histories'].get(active, {}).items():
        if scope not in allowed or not records:
            continue
        rows = working.get(scope, [])
        if not isinstance(rows, list) or len(rows) > CAP:
            continue
        if scope not in working and sum(s not in GLOBAL_KEYS for s in working) + len(restored) >= SCOPE_CAP:
            report['skipped_scopes'] += 1
            continue
        out = _restore_rows(rows, records)
        if out != rows:
            restored[scope] = out
            report['restored_scopes'] += 1
            report['restored_entries'] += len(out) - len(rows)
    working = {**{s:r for s,r in restored.items() if s not in working}, **working}
    working.update(restored)
    profiles = {o: {'globals': {}, 'atoms': {ATOM: deepcopy(hist)}}
                for o, hist in result['histories'].items() if o in owners}
    profiles[active] = {'globals': {}, 'atoms': {ATOM: working}}
    engine = deepcopy(result['engine'])
    oid = _digest(key, ['profile', active])
    prior_native = result['native_views'].get(active, {})
    current_view = engine['views'].get(oid, {})
    active_view = deepcopy(current_view)
    for scope, rows in working.items():
        if scope not in allowed or not isinstance(rows, list) or len(rows) > CAP:
            continue
        bucket = _digest(key, ['scope', scope])
        normalized = current_view.get(bucket, [])
        known = {eid: fp for fp, eid in normalized}
        old_raw = prior_native.get(scope, [])
        # Native-only opaque occurrences remain valid anchors between safe ones.
        ids = _merge_order([eid for _, eid in normalized], [eid for _, eid in old_raw])
        by_id = {eid: fp for fp, eid in old_raw}
        by_id.update(known)
        current = defaultdict(list)
        for row in rows:
            body = _shareable(row)
            if body is not None:
                sig = _digest(key, ['record', {'plain': body}])
                current[sig].append(_digest(key, ['record', row]))
        for eid in ids:
            if eid in known and current[known[eid]]:
                by_id[eid] = current[known[eid]].pop(0)
        if ids or bucket in current_view:
            active_view[bucket] = [[by_id[eid], eid] for eid in ids]
    if active_view or oid in engine['views']:
        engine['views'][oid] = active_view
    engine, merged = sync_history(profiles, engine, {'threads': allowed})
    for name, count in merged.items():
        report['merge_' + name] = count
    output = profiles[active]['atoms'][ATOM]
    report['native_scopes_changed'] = sum(original.get(s) != output.get(s)
                                          for s in original.keys() | output.keys())
    # Cache each owner's resulting safe projection. Raw editor records remain in
    # the actual active native state; only fingerprints and occurrence IDs leave it.
    for owner, profile in profiles.items():
        hist = profile['atoms'][ATOM]
        oid = _digest(key, ['profile', owner])
        owner_view = engine['views'].get(oid, {})
        saved = result['histories'].setdefault(owner, {})
        raw_views = {}
        for scope, rows in hist.items():
            if scope not in allowed or not isinstance(rows, list) or len(rows) > CAP:
                continue
            bucket = _digest(key, ['scope', scope])
            bindings = owner_view.get(bucket)
            if bindings is None or len(bindings) != len(rows):
                continue
            if owner == active:
                raw_views[scope] = deepcopy(bindings)
            plain, normalized = [], []
            for row, (_, eid) in zip(rows, bindings):
                body = _shareable(row)
                if body is not None:
                    record = {'plain': body}
                    plain.append(record)
                    normalized.append([_digest(key, ['record', record]), eid])
            if plain:
                saved.pop(scope, None)
                saved[scope] = plain
                owner_view[bucket] = normalized
        result['histories'][owner] = _trim(saved)
        # The underlying history engine normally sees long-lived native state.
        # Here its private per-owner view follows the bounded cache instead.
        keep = {_digest(key, ['scope', scope]) for scope in result['histories'][owner]}
        engine['views'][oid] = {b:r for b,r in owner_view.items() if b in keep}
        if owner == active:
            result['native_views'][owner] = _trim(raw_views)
    result['engine'] = engine
    if original != output:
        atoms[ATOM] = output
    # Observe exactly what is now committed with the returned ledger, avoiding
    # falsely reporting imported records as a later local send.
    seen = result['observed'].setdefault(active, {})
    for scope, rows in output.items():
        if scope in allowed and isinstance(rows, list) and len(rows) <= CAP and rows:
            signatures = [_digest(key, ['record', row]) for row in rows]
            def issue():
                result['capture_sequence'] += 1
                return result['capture_sequence']
            ids, _, _ = _bind(seen.get(scope, []), signatures, issue, scope in seen)
            seen[scope] = [[sig, eid] for sig, eid in zip(signatures, ids)]
    result['observed'][active] = _trim(seen)
    return result, dict(report)


def seed(ledger, receipts, owners, allowed_threads, *, retained_owners=None):
    """Seed absent caches from verified same-policy receipts, newest first.

    The parent must verify receipt consent/generation and authenticated owners.
    Only exact per-thread before arrays are used; deleted/overwritten scopes and
    their relative cross-owner chronology cannot be inferred from these receipts.
    """
    result, _ = _prepare(ledger, owners, retained_owners)
    report = _report()
    allowed = _allowed(allowed_threads)
    for receipt in receipts:
        for change in receipt.get('changes', []):
            owner, path = change.get('owner'), change.get('path')
            if (owner not in owners or change.get('part') != 'atoms' or not isinstance(path, list)
                    or len(path) != 2 or path[0] != ATOM or path[1] not in allowed):
                continue
            before = change.get('before')
            if not isinstance(before, list) or len(before) != 1 or not isinstance(before[0], list):
                continue
            rows = before[0]
            if len(rows) > CAP:
                report['skipped_scopes'] += 1
                continue
            hist = result['histories'].setdefault(owner, {})
            if path[1] in hist or len(hist) >= SCOPE_CAP:
                continue
            plain = [{'plain': b} for r in rows if (b := _shareable(r)) is not None]
            if plain:
                hist[path[1]] = plain
                report['seeded_scopes'] += 1
    return result
