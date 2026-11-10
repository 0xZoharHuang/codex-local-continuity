"""Synchronize local, user-visible composer recall buckets. Never send a prompt."""
import copy
import hashlib
import hmac
import json
import re
import secrets
from collections import Counter, defaultdict
from difflib import SequenceMatcher


ATOM = 'prompt-history'
GLOBAL_KEYS = frozenset(('global', 'chatgpt-global'))
UUID = re.compile(r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$')
CAP = 20
SCOPE_CAP = 100
# Explicitly detectable credentials or attachment references. User-typed text
# can contain other sensitive material; enrollment is local to this macOS user.
SENSITIVE = re.compile(
    r'-----BEGIN [^\n]{0,60}PRIVATE KEY-----|\b(?:sk-|gh[pousr]_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{12,}'
    r'|\bBearer\s+[A-Za-z0-9._~+/-]{12,}|\beyJ[\w-]{8,}\.eyJ[\w-]{8,}\.[\w-]{8,}'
    r'|(?:password|passwd|api[_-]?key|access[_-]?token|secret[_-]?key)\s*[:=]\s*[^\s]{4,}'
    r'|\b(?:file|sandbox|plugin)://|\bhttps?://[^\s/@]+:[^\s/@]+@'
    r'|[?&](?:sig|signature|token|access_token|api_key|x-amz-signature)=[^\s&]+'
    r'|!\[[^\]\n]*\]\(|<(?:(?:local_)?image|attachment|file_ref|file_mention)\b',
    re.IGNORECASE,
)


def _safe_body(text):
    return isinstance(text, str) and bool(text.strip()) and not SENSITIVE.search(text)


def _visible(node, parent=None):
    """Return paragraph text only for known, fully understood editor shapes."""
    if not isinstance(node, dict) or set(node) - {'type', 'attrs', 'marks', 'content', 'text'}:
        raise ValueError('unsupported editor node')
    typ = node.get('type')
    allowed = {None: ('doc',), 'doc': ('paragraph',), 'paragraph': ('text', 'hard_break')}
    if typ not in allowed.get(parent, ()):
        raise ValueError('unsupported node type')
    attrs = node.get('attrs', {})
    if not isinstance(attrs, dict) or set(attrs) - ({'start', 'end', 'paragraphGap'} if typ == 'paragraph' else set()):
        raise ValueError('unsupported editor attributes')
    if any(value is not None and type(value) not in (bool, int, float) for value in attrs.values()):
        raise ValueError('opaque editor attribute')
    marks = node.get('marks', [])
    if not isinstance(marks, list):
        raise ValueError('unsupported marks')
    for mark in marks:
        if not isinstance(mark, dict) or set(mark) - {'type', 'attrs'}:
            raise ValueError('unsupported mark')
        mt, ma = mark.get('type'), mark.get('attrs', {})
        if mt in ('literalPaste', 'strong') and ma == {}:
            continue
        if mt == 'startend' and isinstance(ma, dict) and set(ma) <= {'start', 'end'} and all(type(v) is int for v in ma.values()):
            continue
        raise ValueError('link, reference or unknown mark')
    if typ in ('hard_break', 'text'):
        if 'content' in node or (typ == 'hard_break' and 'text' in node):
            raise ValueError('invalid inline node')
        if typ == 'hard_break':
            return '\n'
        if not isinstance(node.get('text'), str):
            raise ValueError('invalid text')
        return node['text']
    children = node.get('content', [])
    if not isinstance(children, list) or 'text' in node:
        raise ValueError('invalid children')
    rendered = [_visible(n, typ) for n in children]
    return rendered if typ == 'doc' else ''.join(rendered)


def _shareable(record):
    if isinstance(record, str):
        body = record
    elif isinstance(record, dict) and set(record) == {'plain'}:
        body = record['plain']
    elif isinstance(record, dict) and set(record) in ({'document', 'markdown'}, {'document', 'plain'}):
        body = record['plain'] if 'plain' in record else record['markdown']
        try:
            paragraphs = _visible(record['document'])
            # Accept only if serialization and visible content agree exactly.
            # Native documents with different serializers or invisible refs stay local.
            if body not in ('\n'.join(paragraphs), '\n\n'.join(paragraphs)):
                return None
        except (ValueError, TypeError, RecursionError):
            return None
    else:
        return None
    return body if _safe_body(body) else None


def _digest(key, value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode('ascii')
    return hmac.new(key, encoded, hashlib.sha256).hexdigest()


def _bind(prev_rows, fingerprints, issue, known_owner):
    """Recover occurrence IDs. The native operation is suffix retention + append."""
    prior = [row[0] for row in prev_rows]
    if prior == fingerprints:
        return [row[1] for row in prev_rows], set(), False
    if not prior:
        ids = [issue() for _ in fingerprints]
        return ids, set(ids) if known_owner else set(), False
    if len(fingerprints) < len(prior):
        # A shorter subsequence can result from a clear or multiple deletions;
        # it cannot be a native append. Do not announce retained duplicates as
        # fresh sends. Match from the tail, as the native recall window does.
        pos = len(prior) - 1
        kept = []
        for sig in reversed(fingerprints):
            while pos >= 0 and prior[pos] != sig:
                pos -= 1
            if pos < 0:
                break
            kept.append(prev_rows[pos][1])
            pos -= 1
        if len(kept) == len(fingerprints):
            return list(reversed(kept)), set(), True
    for overlap in range(min(len(prior), len(fingerprints)), 0, -1):
        # Native append drops its oldest prefix only at capacity.
        if overlap != len(prior) and len(fingerprints) != CAP:
            continue
        if prior[-overlap:] == fingerprints[:overlap]:
            new_ids = [issue() for _ in fingerprints[overlap:]]
            return [r[1] for r in prev_rows[-overlap:]] + new_ids, set(new_ids), False
    # Arbitrary edits, restored snapshots, a fully rotated window, or a clear.
    # Retain matching contiguous blocks; ambiguous records may fill spare
    # capacity but must not evict other targets' records as if newly submitted.
    ids = [None] * len(fingerprints)
    for block in SequenceMatcher(a=prior, b=fingerprints, autojunk=False).get_matching_blocks():
        for j in range(block.size):
            ids[block.b+j] = prev_rows[block.a+j][1]
    return [issue() if i is None else i for i in ids], set(), True


def _merge_order(existing, incoming):
    """Insert new occurrences around shared anchors, keeping every original in order."""
    original = set(existing)
    result = list(existing)
    # For wholly disjoint older histories, keep the target's originals newest.
    cursor = 0
    pending = []
    for eid in incoming:
        if eid in original:
            pos = result.index(eid)
            result[pos:pos] = pending
            cursor = pos + len(pending) + 1
            pending = []
        else:
            pending.append(eid)
    result[cursor:cursor] = pending
    return result


def _adopt(bodies, order, pool, fresh, issue):
    """Align pre-enrollment snapshots, which have no stable send IDs or timestamps.

    First match equal runs, starting at the most recent end of the window.
    Then match remaining occurrences with equal visible text one-to-one. A
    profile with more repetitions keeps them all, and conflicting local order
    and rich originals stay intact. Never equate newly observed post-enrollment
    sends with historical copies. Each source window is bounded to CAP entries.
    """
    candidates = [eid for eid in reversed(order) if eid not in fresh]
    reverse_bodies = list(reversed(bodies))
    ids = [None] * len(bodies)
    used = set()
    matcher = SequenceMatcher(a=[pool[eid] for eid in candidates], b=reverse_bodies, autojunk=False)
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            eid = candidates[block.a + offset]
            ids[block.b + offset] = eid
            used.add(eid)
    spare = defaultdict(list)
    for eid in reversed(candidates):
        if eid not in used:
            spare[pool[eid]].append(eid)
    for i, body in enumerate(reverse_bodies):
        if ids[i] is None and body is not None and spare[body]:
            ids[i] = spare[body].pop()
    return [issue() if eid is None else eid for eid in reversed(ids)]


def sync_history(profiles, previous, inventory):
    """Mutate only eligible prompt-history atom scopes; return private baseline and counts.

    profiles maps stable owner keys to dictionaries with globals and atoms. The
    baseline must be committed/recovered with the renderer write. It contains
    salted fingerprints and occurrence IDs, never prompt bodies.
    """
    if previous is None:
        baseline = {'version': 1, 'key': secrets.token_hex(32), 'sequence': 0, 'views': {}}
    else:
        if previous.get('version') != 1:
            raise ValueError('unsupported prompt-history baseline')
        baseline = copy.deepcopy(previous)
    key = bytes.fromhex(baseline['key'])
    if len(key) != 32:
        raise ValueError('invalid prompt-history key')
    views = baseline['views']
    next_id = baseline['sequence']

    def issue():
        nonlocal next_id
        next_id += 1
        return next_id

    allowed = {t for t in inventory['threads'] if isinstance(t, str) and UUID.fullmatch(t)}
    counts = Counter({k: 0 for k in ('scopes_changed', 'scopes_added', 'entries_copied', 'aged_out',
                                      'opaque_records', 'skipped_scopes', 'ambiguous_windows')})
    owners = {}
    scopes = set()
    for name in sorted(profiles):
        atoms = profiles[name]['atoms']
        hist = atoms.get(ATOM, {})
        if not isinstance(hist, dict):
            counts['skipped_scopes'] += 1
            continue  # Older global-only array, or an unknown native shape.
        owner = _digest(key, ['profile', name])
        was_known = owner in views
        owners[owner] = (atoms, hist, was_known)
        views.setdefault(owner, {})
        scopes.update(t for t in hist if t in allowed)
    new_buckets = {owner: {} for owner in owners}
    for scope in sorted(scopes):
        bucket = _digest(key, ['scope', scope])
        initial_sequence = next_id
        own = {}
        pool = {}
        fresh_ids = set()
        order = []
        source_orders = []
        # Bind known occurrences before adopting an older account's preexisting
        # view. A longer cold window supplies more anchors for partial windows.
        def priority(owner):
            _, history, known = owners[owner]
            records = history.get(scope)
            return (not known, -(len(records) if isinstance(records, list) else 0))

        for owner in sorted(owners, key=priority):
            _, hist, known = owners[owner]
            if scope not in hist:
                continue
            records = hist[scope]
            if not isinstance(records, list) or len(records) > CAP:
                counts['skipped_scopes'] += 1
                continue
            # Signature is the native record (not just the displayed string).
            fp = [_digest(key, ['record', r]) for r in records]
            prior = views[owner].get(bucket, [])
            bodies = [_shareable(rec) for rec in records]
            if known:
                ids, fresh, changed = _bind(prior, fp, issue, known)
                fresh_ids.update(fresh)
                counts['ambiguous_windows'] += int(changed)
            else:
                ids = _adopt(bodies, order, pool, fresh_ids, issue)
            entries = []
            for rec, sig, eid, body in zip(records, fp, ids, bodies):
                entries.append((rec, sig, eid, body is not None))
                if body is None:
                    counts['opaque_records'] += 1
                else:
                    pool[eid] = body
            own[owner] = entries
            sequence = [eid for eid, body in zip(ids, bodies) if body is not None]
            source_orders.append(sequence)
            order = _merge_order(order, sequence)

        for owner, (atoms, hist, _) in owners.items():
            present = scope in hist
            if present and owner not in own:
                continue
            # Missing previously enrolled buckets may have been pruned or cleared.
            if not present and (bucket in views[owner] or not pool):
                continue
            if not present and sum(k not in GLOBAL_KEYS for k in hist) + len(new_buckets[owner]) >= SCOPE_CAP:
                counts['skipped_scopes'] += 1
                continue
            local = own.get(owner, [])
            present_ids = {e[2] for e in local}
            candidates = [eid for eid in order if eid not in present_ids]
            # A manual removal or native truncation must not silently repopulate
            # old occurrences from another profile. After a target has enrolled,
            # only newly observed peer events are eligible to fill spare slots.
            enrolled = bucket in views[owner]
            older = [eid for eid in candidates
                     if eid not in fresh_ids and (not enrolled or eid > initial_sequence)]
            newer = sorted(eid for eid in candidates if eid in fresh_ids)
            # Keep the most recent eligible foreign events if the new batch exceeds
            # capacity. Opaque target originals are never evicted by this module.
            room_for_new = CAP - sum(not entry[3] for entry in local)
            newer = newer[-room_for_new:] if room_for_new else []
            evict = max(0, len(local) + len(newer) - CAP)
            if evict:
                oldest = {r[2] for r in [x for x in local if x[3]][:evict]}
                local = [r for r in local if r[2] not in oldest]
                counts['aged_out'] += len(oldest)
            room_for_old = CAP - len(local) - len(newer)
            older = older[-room_for_old:] if room_for_old else []

            def transferred(eid):
                rec = {'plain': pool[eid]}
                return rec, _digest(key, ['record', rec]), eid, True

            result = local
            if older:
                by_id = {r[2]: r for r in local}
                by_id.update((i, transferred(i)) for i in older)
                sequence = [r[2] for r in local]
                for source_order in source_orders:
                    sequence = _merge_order(sequence, [i for i in source_order if i in by_id])
                result = [by_id[i] for i in sequence]
            result = result + [transferred(i) for i in newer]
            records = [r[0] for r in result]
            if present:
                if hist[scope] != records:
                    hist[scope] = records
                    counts['scopes_changed'] += 1
            else:
                if not records:
                    continue
                new_buckets[owner][scope] = records
                counts['scopes_added'] += 1
            counts['entries_copied'] += len(older) + len(newer)
            views[owner][bucket] = [[r[1], r[2]] for r in result]
    for owner, (atoms, hist, _) in owners.items():
        if new_buckets[owner]:
            # Native insertion order is used when evicting an old scope.
            atoms[ATOM] = {**new_buckets[owner], **hist}
    baseline['sequence'] = next_id
    return baseline, dict(counts)
