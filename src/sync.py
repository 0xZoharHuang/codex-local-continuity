#!/usr/bin/python3
"""Offline, per-user continuity for Codex's native local projects and composer history."""
import argparse
import contextlib
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid

# The installed watcher invokes isolated system Python, with no user site or PYTHONPATH.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from history import sync_history
from projects import sync_projects
import native_layout

ROOT = Path.home() / "Library/Application Support/CodexLocalContinuity"
CODEX = Path.home() / ".codex"
GLOBAL = ".codex-global-state.json"
SCOPES = "electron-account-renderer-state-v1"
ATOMS = "electron-persisted-atom-state"
# Only these renderer fields may cross profile boundaries.
PROJECT_KEYS = {
    "local-projects", "app-server-project-id-by-legacy-project-id-by-host",
    "thread-project-assignments", "projectless-thread-ids", "thread-project-membership-host-ids",
    "sidebar-project-thread-orders", "project-order",
}
VIEW_PREFS = "flat-project-sidebar-preferences-v1"
FOLDER_KEYS = {"local-projects","app-server-project-id-by-legacy-project-id-by-host","project-order"}
ATOM_KEYS = {"prompt-history", VIEW_PREFS}
FIELDS = {"globals": PROJECT_KEYS, "atoms": ATOM_KEYS}
MARKER = Path.home() / "Library/Application Support/Codex/SingletonLock"
MAX_BACKUPS = 5
MAX_ARCHIVED_GENERATIONS = 5
FEATURES = ('projects', 'history', 'both', 'off', 'folders', 'folders-history')
GENERATION = re.compile(r'[0-9]{20}-[0-9a-f]{32}')
MISSING = object()


class Refusal(Exception):
    """Messages must be static codes, never values from native settings."""


def need(condition, code):
    if not condition:
        raise Refusal(code)


def require_default_home():
    override = os.environ.get("CODEX_HOME")
    need(not override or Path(override).expanduser().resolve() == CODEX,
         "only-default-CODEX_HOME-supported")


def packed(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode()


def as_json(value):
    # Keep native object insertion order; some native UIs rely on that order.
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def fingerprint(raw):
    return hashlib.sha256(raw).hexdigest()


def digest_value(value):
    return fingerprint(packed([] if value is MISSING else [value]))


def read_json(raw):
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            need(key not in obj, "duplicate-json-key")
            obj[key] = value
        return obj
    return json.loads(raw, object_pairs_hook=unique)


def check_file(path, private=False):
    item = path.lstat()
    need(stat.S_ISREG(item.st_mode) and item.st_uid == os.getuid() and item.st_nlink == 1
         and not (item.st_mode & (0o077 if private else 0o022)), "unsafe-file")
    return item


def read_regular(path, private=False):
    before = check_file(path, private)
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        current = os.fstat(fd)
        need((current.st_dev, current.st_ino) == (before.st_dev, before.st_ino), "file-replaced")
        raw = f.read()
        after = os.fstat(fd)
        need((after.st_size, after.st_mtime_ns) == (current.st_size, current.st_mtime_ns), "file-changing")
        return raw


def directory_sync(directory):
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def staged_file(path, raw, mode=0o600):
    fd, name = tempfile.mkstemp(prefix=".continuity-", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(fd, mode)
            f.write(raw)
            f.flush()
            os.fsync(fd)
        yield temp
    finally:
        temp.unlink(missing_ok=True)


def replace(path, raw):
    with staged_file(path, raw) as temp:
        os.replace(temp, path)
        directory_sync(path.parent)


def offline(previous_pid=0):
    # A newly starting instance can create its singleton before AppKit emits a launch event.
    def singleton_permits():
        try:
            info = MARKER.lstat()
        except FileNotFoundError:
            return True
        if (previous_pid < 2 or not stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid()
                or os.readlink(MARKER) != f"{socket.gethostname()}-{previous_pid}"):
            return False
        try:
            os.kill(previous_pid, 0)
        except ProcessLookupError:
            return True
        return False

    if not singleton_permits():
        return False
    # Another GUI user's app has its own home and does not write our file.
    result = subprocess.run(["/bin/ps", "-ww", "-U", str(os.getuid()), "-o", "comm="], capture_output=True,
                            check=True, text=True, timeout=5)
    # If our UID is running a same-named app from an unrecognized location,
    # conservatively defer, including if the user replaced or moved its bundle.
    return not any(p.strip().endswith(("/Contents/MacOS/ChatGPT", "/Contents/MacOS/Codex"))
                   for p in result.stdout.splitlines()) and singleton_permits()


def check_native_metadata(path):
    st = check_file(path)
    need(bool(st.st_mode & stat.S_IWUSR) and not getattr(st, "st_flags", 0),
         "nonstandard-native-file-protection")
    if sys.platform == 'darwin':
        query = subprocess.run(['/usr/bin/xattr','-s',str(path)], check=True,
                               capture_output=True, text=True, timeout=5)
        attrs = set(query.stdout.splitlines())
        need(not attrs - {'com.apple.provenance'}, "custom-native-file-attributes")
        listing = subprocess.run(['/bin/ls','-lde',str(path)], check=True,
                                 capture_output=True, text=True, timeout=5)
        need(not re.search(r'^\s*\d+:', listing.stdout, re.M), "custom-native-file-acl")


def inspect_layout(state):
    try:
        return native_layout.inspect(state)
    except ValueError as error:
        raise Refusal(str(error)) from None


def profile_refs(state):
    need(inspect_layout(state)['kind']=='legacy', 'legacy-recovery-incompatible-with-native-layout')
    wrap = state[SCOPES]
    need(isinstance(wrap, dict) and 'owner' in wrap and isinstance(wrap.get('inactive'), dict),
         "unsupported-identity-layout")
    rows = [(o, v['globals'], v['atoms']) for o, v in wrap['inactive'].items()]
    if wrap['owner'] is not None:
        rows.append((wrap['owner'], state, state[ATOMS]))
    profiles = {}
    skipped = 0
    for owner, g, a in rows:
        need(isinstance(owner, str) and re.fullmatch(r"[0-9a-f]{64}", owner)
             and isinstance(g, dict) and isinstance(a, dict), "unsupported-profile-layout")
        identity = a.get("local-document-identity")
        if not isinstance(identity, dict) or not all(isinstance(identity.get(k), str)
                                                    and identity[k] for k in ("userId", "accountId")):
            skipped += 1
            continue
        # Electron scopes by SHA256(JSON.stringify([userId,accountId])).
        need(fingerprint(as_json([identity["userId"], identity["accountId"]])) == owner,
             "identity-proof-mismatch")
        need(owner not in profiles, "duplicate-identity")
        profiles[owner] = {"globals": g, "atoms": a}
    return profiles, skipped


def make_policy(state, selection, include_history=False, features=None):
    refs = inspect_layout(state)['owners']
    need(bool(refs), "no-initialized-profiles")
    indexes = {o[:12]: o for o in refs}
    need(len(indexes) == len(refs), "ambiguous-profile-label")
    if selection == "all":
        scope, owners = "all", []
    elif selection in ("existing", "current"):
        scope, owners = "selected", sorted(refs)
    else:
        labels = [s.strip() for s in selection.split(",")]
        need(len(set(labels)) == len(labels) and all(s in indexes for s in labels), "unknown-profile-label")
        scope, owners = "selected", sorted(indexes[s] for s in labels)
    if features is not None:
        need(not include_history, "ambiguous-feature-selection")
        return validate_policy({"version":2, "scope":scope, "owners":owners, "features":features})
    return validate_policy({"version": 1, "scope": scope, "owners": owners,
                            "include_history": include_history})


def validate_policy(policy):
    need(isinstance(policy, dict) and type(policy.get("version")) is int and (
         (policy["version"] == 1 and set(policy) == {"version","scope","owners","include_history"}
          and type(policy["include_history"]) is bool) or
         (policy["version"] == 2 and set(policy) == {"version","scope","owners","features"}
          and policy["features"] in FEATURES)) and policy["scope"] in ("selected","all"),
         "missing-or-unsupported-enrollment-policy")
    owners = policy["owners"]
    need(isinstance(owners, list) and
         all(isinstance(o, str) and re.fullmatch(r"[0-9a-f]{64}", o) for o in owners)
         and owners == sorted(set(owners)) and bool(owners) == (policy["scope"] == "selected"),
         "invalid-enrollment-owners")
    return policy


def policy_features(policy):
    validate_policy(policy)
    if policy['version'] == 2:
        return policy['features']
    return 'both' if policy['include_history'] else 'projects'


def projects_enabled(policy):
    return policy_features(policy) in ('both','projects','folders','folders-history')


def history_enabled(policy):
    return policy_features(policy) in ('both','history','folders-history')


def policy_fields(policy):
    mode = policy_features(policy)
    full = mode in ('both','projects')
    globals_ = PROJECT_KEYS if full else FOLDER_KEYS if mode in ('folders','folders-history') else set()
    atoms = {VIEW_PREFS} if full else set()
    if history_enabled(policy):
        atoms.add('prompt-history')
    return {'globals':set(globals_), 'atoms':atoms}


def equivalent(a,b):
    return (policy_features(a),a['scope'],a['owners']) == (policy_features(b),b['scope'],b['owners'])


def legacy_policy(policy):
    return policy["scope"] == "all" and policy_features(policy) == "both"


def guard_scope(record, policy):
    # Only the explicit historical full-scope choice can adopt pre-policy recovery data.
    if "policy" in record:
        accepted = {fingerprint(packed(policy))}
        # Preserve existing v1 receipts only for their identical feature/scope choice.
        if policy_features(policy) in ('projects','both'):
            old = {"version":1,"scope":policy["scope"],"owners":policy["owners"],
                   "include_history":history_enabled(policy)}
            accepted.add(fingerprint(packed(old)))
        need(record["policy"] in accepted, "enrollment-policy-mismatch")
    elif any(k in record for k in ("projects", "history", "changes")):
        need(legacy_policy(policy), "legacy-state-requires-explicit-all-and-history")
    need((history_enabled(policy) or 'history' not in record) and
         (projects_enabled(policy) or 'projects' not in record), "recovery-outside-enrollment")
    permitted = policy_fields(policy)
    if permitted['globals'] == FOLDER_KEYS and 'projects' in record:
        # A compatible fingerprint is insufficient if a damaged/misfiled
        # baseline would feed chat assignments or orders back into the reducer.
        p = record['projects']
        views = list(p.get('owners', {}).values())
        maps = [p.get('canonical', {}),p.get('conflicts', {})] + [v['data'] for v in views]
        need(all(k == 'order' or k.startswith('p/') for m in maps for k in m)
             and all(set(v['fields']) <= FOLDER_KEYS for v in views), 'recovery-outside-enrollment')
    for item in record.get("changes", ()):
        need((policy["scope"] == "all" or item["owner"] in policy["owners"])
             and item.get("path") and item["part"] in permitted and item["path"][0] in permitted[item["part"]],
             "recovery-outside-enrollment")


def local_inventory(codex, include_threads=True, *, include_projects=True, verify_history=True):
    versions = [int(m.group(1)) for f in codex.glob('state_*.sqlite')
                for m in [re.fullmatch(r'state_(\d+)\.sqlite', f.name)] if m]
    need(bool(versions) and max(versions) == 5, "unrecognized-native-database-version")
    path = codex / "state_5.sqlite"
    check_file(path)
    wal, shm = (codex / (path.name + suffix) for suffix in ("-wal", "-shm"))
    for f in (wal, shm):
        if os.path.lexists(f):
            check_file(f)
    live_wal = wal.exists() and shm.exists()
    need(live_wal or not wal.exists() or wal.stat().st_size == 0, "database-not-ready")
    uri = path.as_uri() + ("?mode=ro" if live_wal else "?mode=ro&immutable=1")
    with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=2)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        projects = {}
        all_ids = set()
        aliases = {}
        if include_projects:
            for pid, name, metadata in db.execute("SELECT id,name,metadata FROM projects"):
                all_ids.add(pid)
                if metadata is not None and json.loads(metadata) == {}:
                    projects[pid] = {"name": name, "roots": []}
            for pid, path in db.execute("SELECT project_id,path FROM project_roots ORDER BY project_id,position"):
                if pid in projects:
                    projects[pid]["roots"].append(path)
            aliases = {k: v for k, v in db.execute("SELECT key,project_id FROM project_idempotency_keys")
                       if v in projects}
        threads = set()
        history_threads = set()
        rows = db.execute(
            "SELECT id,thread_source,rollout_path FROM threads WHERE source IN ('cli','vscode') "
            "AND agent_role IS NULL AND agent_path IS NULL AND attached_thread_parent_id IS NULL"
        ) if include_threads else ()
        for tid, src, rollout in rows:
            if (not isinstance(rollout, str) or src not in (
                None, "user", "agent_created_thread", "chatgpt_handoff", "realtime_voice", "voice_chat"
            )):
                continue
            parts = Path(rollout)
            if (str(parts) != os.path.normpath(rollout) or parts.suffix != '.jsonl' or
                    not any(base in parts.parents for base in (codex/'sessions', codex/'archived_sessions'))):
                continue
            threads.add(tid)
            if not verify_history or src == "chatgpt_handoff":
                continue
            # A stale thread index is sufficient to preserve an existing sidebar
            # assignment, but should not fan cached input out to another profile.
            try:
                st = parts.lstat()
                present = (stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid()
                           and parts.resolve(strict=True) == parts)
            except (OSError, RuntimeError):
                present = False
            if present:
                history_threads.add(tid)
    return {"host": "local:" + str(codex), "threads": threads, "history_threads": history_threads,
            "projects": projects, "all_project_ids": all_ids, "aliases": aliases}


def policy_inventory(codex, policy):
    mode = policy_features(policy)
    if mode == 'folders':
        return local_inventory(codex, include_threads=False)
    if mode == 'history':
        return local_inventory(codex, include_projects=False)
    if mode == 'projects':
        return local_inventory(codex, verify_history=False)
    need(mode != 'off', 'native-inventory-disabled')
    return local_inventory(codex)


def project_state(refs, include_history=True, include_projects=True, fields=None):
    return {owner: {part: {k: copy.deepcopy(r[part][k]) for k in keys if k in r[part]
                           and (include_history if part == 'atoms' and k == "prompt-history" else include_projects)}
                    for part, keys in (FIELDS if fields is None else fields).items()} for owner, r in refs.items()}


def changed_nodes(owner, part, path, old, new):
    if old == new:
        return
    if old is MISSING and isinstance(new, dict):
        # Track creation separately from its children. A new sibling written by
        # Codex later must survive reverting the original insertions.
        yield {"owner": owner, "part": part, "path": path, "before": [], "created_map": True}
        for key, value in new.items():
            yield from changed_nodes(owner, part, path + [key], MISSING, value)
        return
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(old.keys() | new.keys()):
            yield from changed_nodes(owner, part, path + [key], old.get(key, MISSING), new.get(key, MISSING))
        return
    yield {"owner": owner, "part": part, "path": path, "before": [] if old is MISSING else [old],
           "after": digest_value(new)}


def compute(state, baseline, inventory, policy):
    validate_policy(policy)
    guard_scope(baseline, policy)
    if policy_features(policy) == "off":
        return baseline, [], {"projects":{"enabled":False},"history":{"enabled":False},"changed_fields":0}
    refs, skipped = profile_refs(state)
    need(refs, "no-initialized-profiles")
    if policy["scope"] == "selected":
        refs = {o: r for o, r in refs.items() if o in policy["owners"]}
    need(refs, "no-enrolled-profile-available")
    if len(refs) < 2:
        return baseline, [], {"profiles":len(refs),"uninitialized":skipped,"changed_fields":0,
                              "projects":{"enabled":projects_enabled(policy)},"history":{"enabled":history_enabled(policy)}}
    permitted = policy_fields(policy)
    original = project_state(refs, fields=permitted)
    profiles = copy.deepcopy(original)
    series, project_counts = None, {"enabled":False}
    if projects_enabled(policy):
        projects_inventory = inventory
        catalog_views = profiles
        if permitted['globals'] == FOLDER_KEYS:
            projects_inventory = {k:v for k,v in inventory.items() if k not in ('threads','history_threads')}
            projects_inventory['threads'] = set()
            catalog_views = {o:{'globals':p['globals'], 'atoms':{}} for o,p in profiles.items()}
        series, project_counts = sync_projects(catalog_views, baseline.get("projects"), projects_inventory)
    # New native profiles should show the shared projects at first enrollment.
    # An absent preference already defaults to project grouping in this format.
    prior_owners = (baseline.get("projects") or {}).get("owners", {})
    for owner, profile in profiles.items():
        pref = profile["atoms"].get(VIEW_PREFS)
        if (VIEW_PREFS in permitted['atoms'] and owner not in prior_owners and isinstance(pref, dict) and
                pref.get("mode") in ("list", "connection") and profile["globals"].get("local-projects")):
            profile["atoms"][VIEW_PREFS] = dict(pref, mode="project")
    histories, history_counts = None, {"enabled": False}
    if history_enabled(policy):
        history_inventory = dict(inventory, threads=inventory.get("history_threads", inventory["threads"]))
        histories, history_counts = sync_history(profiles, baseline.get("history"), history_inventory)
    changes = []
    for owner, profile in profiles.items():
        for part, keys in permitted.items():
            need(set(profile[part]) <= keys, "unexpected-output-field")
            for k in keys:
                old, new = original[owner][part].get(k, MISSING), profile[part].get(k, MISSING)
                if old == new:
                    continue
                changes.extend(changed_nodes(owner, part, [k], old, new))
                if new is MISSING:
                    del refs[owner][part][k]
                else:
                    refs[owner][part][k] = new
    counts = {"profiles": len(refs), "uninitialized": skipped, "changed_fields":len(changes),
              "projects":project_counts, "history":history_counts}
    nextbase = {"version":1, "policy":fingerprint(packed(policy))}
    if projects_enabled(policy):
        nextbase["projects"] = series
    if history_enabled(policy):
        nextbase["history"] = histories
    guard_scope(nextbase, policy)
    return nextbase, changes, counts


def managed_node(refs, change):
    need(change['owner'] in refs, 'profile-unavailable')
    obj = refs[change['owner']][change['part']]
    path = change['path']
    need(bool(path) and path[0] in FIELDS[change['part']], 'unexpected-backup-field')
    for key in path[:-1]:
        if key not in obj or not isinstance(obj[key], dict):
            raise Refusal('restore-point-diverged')
        obj = obj[key]
    return obj, path[-1]


def compare_nodes(state, changes, side):
    refs, _ = profile_refs(state)
    for change in changes:
        try:
            node, key = managed_node(refs, change)
        except Refusal as error:
            if str(error) == 'restore-point-diverged' and side == 'before' and not change['before']:
                continue
            return False
        if change.get('created_map'):
            value = node.get(key, MISSING)
            if (side == 'after' and not isinstance(value, dict)) or (side == 'before' and value is not MISSING):
                return False
            continue
        expected = (change['after'] if side == 'after' else
                    digest_value(change['before'][0] if change['before'] else MISSING))
        if digest_value(node.get(key, MISSING)) != expected:
            return False
    return True


def restore_nodes(state, changes):
    need(compare_nodes(state, changes, "after"), "restore-point-diverged")
    refs, _ = profile_refs(state)
    for change in reversed(changes):
        obj, key = managed_node(refs, change)
        if change.get('created_map'):
            if not obj[key]:
                del obj[key]
        elif change['before']:
            obj[key] = change['before'][0]
        else:
            obj.pop(key, None)


def numbers_only(obj):
    if isinstance(obj, dict):
        return {k: numbers_only(v) for k, v in obj.items() if isinstance(k, str)
                and re.fullmatch(r"[a-z][a-z0-9_-]{0,50}", k)}
    return obj if type(obj) in (bool, int) else None


class Store:
    def __init__(self, root=ROOT, codex=CODEX, is_offline=None):
        self.data = root/'data'
        self.enrollment = self.data/'policy.json'
        self.pending = self.data/'pending.json'
        self.generations = self.data/'generations'
        self.codex = codex
        self.live = codex/GLOBAL
        self.is_offline = is_offline if is_offline is not None else lambda: offline(
            int(os.environ.get("CODEX_CONTINUITY_EXIT_PID") or 0))

    @staticmethod
    def _private_dir(directory):
        st = directory.lstat()
        need(directory.resolve() == directory and stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid()
             and not (st.st_mode & 0o077), "unsafe-directory")

    def _location(self):
        wrapper = self.load(self.enrollment, None)
        if not isinstance(wrapper, dict) or 'generation' not in wrapper:
            return wrapper, self.data, None
        need(set(wrapper) == {'generation','policy'} and isinstance(wrapper['generation'],str)
             and GENERATION.fullmatch(wrapper['generation']), 'unsafe-enrollment-generation')
        name = wrapper['generation']
        area = self.generations/name
        self._private_dir(self.generations)
        self._private_dir(area)
        self._private_dir(area/'backups')
        need(self.load(area/'enrollment.json', None) == wrapper, 'generation-policy-mismatch')
        return wrapper['policy'], area, name

    @property
    def baseline(self):
        return self._location()[1]/'baseline.json'

    @property
    def backups(self):
        return self._location()[1]/'backups'

    @property
    def log(self):
        return self._location()[1]/'status.json'

    def prepare(self, read_only=False, native=True):
        directories = [self.data.parent, self.data, self.backups]
        if native:
            directories.append(self.codex)
        for directory in directories:
            if read_only and directory != self.codex and not os.path.lexists(directory):
                continue
            st = directory.lstat()
            need(directory.resolve() == directory and stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid()
                 and not (st.st_mode & (0o022 if directory == self.codex else 0o077)), "unsafe-directory")

    def mark(self, code, counts=None):
        event = {"state": code}
        if counts is not None:
            event["counts"] = numbers_only(counts)
        previous = self.load(self.log, {})
        if {k:v for k,v in previous.items() if k != 'time'} != event:
            event["time"] = int(time.time())
            replace(self.log, packed(event))

    @staticmethod
    def load(path, default):
        return read_json(read_regular(path, True)) if os.path.lexists(path) else default

    def policy(self):
        return validate_policy(self._location()[0])

    def propose(self, selection=None, features=None):
        """Omitted options preserve the installed scope and flags. No writes."""
        self.prepare(read_only=True, native=False)
        saved = self.policy() if os.path.lexists(self.enrollment) else None
        if features is None:
            features = policy_features(saved) if saved is not None else 'projects'
        need(features in FEATURES, 'unknown-feature-selection')
        if selection is None:
            need(saved is not None, 'explicit-profile-selection-required')
            proposed = {'version':2, 'scope':saved['scope'], 'owners':saved['owners'][:], 'features':features}
        else:
            self.prepare(read_only=True)
            proposed = make_policy(read_json(read_regular(self.live)), selection, features=features)
        return saved if saved is not None and equivalent(saved, proposed) else validate_policy(proposed)

    def _prune_generations(self, current):
        archives = sorted((d for d in self.generations.iterdir() if GENERATION.fullmatch(d.name)
                           and d.name != current), reverse=True)
        for area in archives[MAX_ARCHIVED_GENERATIONS:]:
            self._private_dir(area)
            files = list(area.iterdir())
            need(all(p.name in ('backups','baseline.json','status.json','enrollment.json',
                               'recall-v2.json','capture-status.json') for p in files),
                 'unexpected-archived-generation-file')
            backups = area/'backups'
            inner = []
            if os.path.lexists(backups):
                self._private_dir(backups)
                inner = list(backups.iterdir())
            need(all(re.fullmatch(r'[0-9]{20}-[0-9a-f]{8}\.json', p.name) for p in inner),
                 'unexpected-archived-backup-file')
            regular = [p for p in files if p.name != 'backups'] + inner
            for p in regular:
                check_file(p, True)
            for p in regular:
                p.unlink()
            if backups.exists():
                backups.rmdir()
            area.rmdir()
        directory_sync(self.generations)

    def reconfigure(self, new_policy):
        """Under the admin and worker locks, atomically publish a fresh consent generation."""
        self.prepare(native=False)
        old, old_area, _ = self._location()
        validate_policy(old)
        validate_policy(new_policy)
        need(not os.path.lexists(self.pending), 'pending-transaction')
        if equivalent(old, new_policy):
            return {'state':'configuration-unchanged'}
        policy = {'version':2,'scope':new_policy['scope'],'owners':new_policy['owners'][:],
                  'features':policy_features(new_policy)}
        if old_area == self.data:
            # Historical install files retain their original paths. Once switched,
            # the public restore route never consults these pre-generation backups.
            legacy = self.data/'legacy-enrollment.json'
            if not os.path.lexists(legacy):
                replace(legacy, packed(old))
            else:
                need(self.load(legacy, None) == old, 'legacy-archive-policy-mismatch')
        self.generations.mkdir(mode=0o700, exist_ok=True)
        self._private_dir(self.generations)
        name = f'{time.time_ns():020}-{uuid.uuid4().hex}'
        area = self.generations/name
        area.mkdir(mode=0o700)
        (area/'backups').mkdir(mode=0o700)
        wrapper = {'policy':policy, 'generation':name}
        replace(area/'enrollment.json', packed(wrapper))
        directory_sync(area)
        directory_sync(self.generations)
        replace(self.enrollment, packed(wrapper))
        report = {'state':'configured','features':policy_features(policy)}
        try:
            self._prune_generations(name)
        except (OSError, Refusal):
            # The consent pointer is already committed. An unexpected file in an
            # old archive must neither undo the choice nor be silently removed.
            report['archive_cleanup'] = 'deferred'
        return report

    def _guard_generation(self, record, generation):
        if record != {'version':1}:
            need(record.get('generation') == generation, 'recovery-generation-mismatch')
            need(generation is None or isinstance(record.get('policy'), str),
                 'generation-policy-mismatch')

    def list_profiles(self):
        self.prepare(read_only=True)
        state = read_json(read_regular(self.live))
        layout = inspect_layout(state)
        refs = layout['owners']
        labels = [o[:12] for o in refs]
        need(len(labels) == len(set(labels)), "ambiguous-profile-label")
        return [{"label": o[:12], "active": o == layout['active']} for o in sorted(refs)]

    def select(self, selection, include_history=False):
        if selection is None:
            self.prepare(read_only=True, native=False)
            need(not include_history, "select-profiles-when-requesting-history")
            return self.policy()
        self.prepare(read_only=True)
        return make_policy(read_json(read_regular(self.live)), selection, include_history)

    def finish(self, tx, applied):
        if applied:
            backup_path = self.backups/(tx['id']+'.json')
            if not backup_path.exists():
                replace(backup_path, as_json({k:tx[k] for k in
                    ('id','before','after','changes','old_baseline','policy','generation') if k in tx}))
            replace(self.baseline, packed(tx['new_baseline']))
            self.mark("restored" if tx.get("rollback") else "synced", tx['counts'])
        self.pending.unlink()
        directory_sync(self.data)
        for old in sorted(self.backups.glob('*.json'), reverse=True)[MAX_BACKUPS:]:
            check_file(old, True)
            old.unlink()

    def recover(self, raw, policy):
        if not os.path.lexists(self.pending):
            return
        tx = self.load(self.pending, None)
        guard_scope(tx, policy)
        gen = self._location()[2]
        self._guard_generation(tx,gen)
        for member in ('old_baseline','new_baseline'):
            self._guard_generation(tx[member],gen)
            guard_scope(tx[member],policy)
        h = fingerprint(raw)
        if h == tx['after']:
            self.finish(tx, True)
        elif h == tx['before']:
            self.finish(tx, False)
        else:
            state = read_json(raw)
            if compare_nodes(state, tx['changes'], "after"):
                self.finish(tx, True)
            elif compare_nodes(state, tx['changes'], "before"):
                self.finish(tx, False)
            else:
                raise Refusal("interrupted-transaction-needs-review")

    def update(self, action, preview_policy=None):
        need(preview_policy is None or action == 'plan', 'policy-override-only-for-preview')
        policy = self.policy() if preview_policy is None else validate_policy(preview_policy)
        if policy_features(policy) == 'off':
            need(action != 'restore', 'restore-disabled-by-policy')
            return {'state':'preview' if action == 'plan' else 'off', 'baseline_would_change':False,
                    'counts':{'changed_fields':0, 'projects':{'enabled':False}, 'history':{'enabled':False}}}
        self.prepare(read_only=action == 'plan')
        generation = self._location()[2]
        raw = read_regular(self.live)
        layout = inspect_layout(read_json(raw))
        if action == 'capture':
            if layout['kind']=='legacy':
                return {'state':'capture-not-required'}
            import modern_sync
            return modern_sync.capture(self)
        if layout['kind']=='shared-v2':
            import modern_sync
            fresh = (preview_policy is not None and os.path.lexists(self.enrollment)
                     and not equivalent(self.policy(),policy))
            return modern_sync.update(self,action,raw,policy,fresh=fresh)
        if action != 'plan':
            need(self.is_offline(), 'app-running-deferred')
            self.recover(raw, policy)
            raw = read_regular(self.live)
        else:
            need(not os.path.lexists(self.pending), "pending-transaction")
        state = read_json(raw)
        if (preview_policy is not None and os.path.lexists(self.enrollment)
                and not equivalent(self.policy(), policy)):
            # Simulate a fresh enrollment. A baseline from other owners must not
            # supply a single field (including a prompt) to this preview.
            base = {'version':1}
            generation = None
        else:
            need(os.path.lexists(self.baseline) or not any(self.backups.glob('*.json')),
                 'missing-baseline-needs-review')
            base = self.load(self.baseline, {'version':1})
            need(isinstance(base, dict) and base.get('version') == 1, 'unsupported-helper-state')
        self._guard_generation(base,generation)
        guard_scope(base, policy)
        if action == 'restore':
            backups = sorted(self.backups.glob('*.json'), reverse=True)
            need(backups, 'no-recovery-point')
            b = self.load(backups[0], None)
            self._guard_generation(b,generation)
            guard_scope(b, policy)
            self._guard_generation(b['old_baseline'],generation)
            guard_scope(b['old_baseline'],policy)
            refs = profile_refs(state)[0]
            if policy["scope"] == "selected":
                refs = {o:r for o,r in refs.items() if o in policy["owners"]}
            permitted = policy_fields(policy)
            original = project_state(refs, fields=permitted)
            restore_nodes(state, b['changes'])
            restored = project_state(refs, fields=permitted)
            changes = [item for o in original for part in FIELDS
                       for k in original[o][part].keys() | restored[o][part].keys()
                       for item in changed_nodes(o, part, [k], original[o][part].get(k, MISSING),
                                                 restored[o][part].get(k, MISSING))]
            nextbase, counts = b['old_baseline'], {'changed_fields':len(changes)}
        else:
            inventory = policy_inventory(self.codex,policy)
            nextbase, changes, counts = compute(state, base, inventory, policy)
        if generation is not None and nextbase != {'version':1}:
            nextbase['generation'] = generation
        if action == 'plan':
            return {"state":"preview","counts":numbers_only(counts),"baseline_would_change":nextbase!=base}
        if not changes:
            if nextbase != base:
                need(self.is_offline() and read_regular(self.live)==raw, 'source-changed-deferred')
                replace(self.baseline, packed(nextbase))
                self.mark('baseline-updated', counts)
            elif self.load(self.log, {}).get('state') not in (
                    'synced','baseline-updated','restored','no-native-changes'):
                self.mark('no-native-changes', counts)
            return {"state":"no-native-changes","counts":numbers_only(counts)}
        output = as_json(state)
        need(read_json(output) == state, 'json-roundtrip-failed')
        receipt = {"id":f"{time.time_ns():020}-{uuid.uuid4().hex[:8]}","before":fingerprint(raw),
                   "after":fingerprint(output),"changes":changes,"old_baseline":base,
                   "new_baseline":nextbase,"counts":numbers_only(counts),"rollback":action=='restore',
                   "policy":fingerprint(packed(policy))}
        if generation is not None:
            receipt['generation'] = generation
        check_native_metadata(self.live)
        with staged_file(self.live, output, stat.S_IMODE(check_file(self.live).st_mode)) as temp:
            need(temp.stat().st_gid == self.live.stat().st_gid, 'nonstandard-native-file-group')
            need(read_regular(temp)==output, 'staging-verification-failed')
            need(self.is_offline(), 'app-running-deferred')
            replace(self.pending, as_json(receipt))
            if action == 'sync':
                try:
                    current = policy_inventory(self.codex,policy)
                    stable = current == inventory
                except Exception:
                    self.finish(receipt, False)
                    raise
                if not stable:
                    self.finish(receipt, False)
                    raise Refusal('native-inventory-changed-deferred')
            if not self.is_offline() or read_regular(self.live)!=raw:
                self.finish(receipt, False)
                raise Refusal('source-changed-deferred')
            os.replace(temp, self.live)
            directory_sync(self.codex)
            if read_regular(self.live) != output:
                raise Refusal('post-commit-recovery-needed')
        self.finish(receipt, True)
        return {"state":"restored" if action=='restore' else "synced","counts":numbers_only(counts)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('sync','capture','plan','status','restore'))
    args=parser.parse_args()
    require_default_home()
    os.umask(0o077)
    store=Store()
    store.prepare(native=False)
    if args.action=='status':
        print(json.dumps(store.load(store.log, {'state':'awaiting-first-exit'}), ensure_ascii=False))
        return
    if args.action=='plan':
        print(json.dumps(store.update('plan'), ensure_ascii=False))
        return
    lock_path = store.data/'job.lock'
    fd = os.open(lock_path, os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'a') as lock:
        check_file(lock_path, True)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            result = store.update(args.action)
            print(json.dumps(result, ensure_ascii=False))
        except Exception as error:
            code = str(error) if type(error) is Refusal else 'unrecognized-layout-or-io-' + type(error).__name__
            if args.action=='capture':
                capture_file = store.baseline.parent/'capture-status.json'
                if code != 'capture-source-changed':
                    replace(capture_file,packed({'state':code,'time':int(time.time())}))
            else:
                store.mark(code)
            print(code, file=sys.stderr)
            sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Do not log cache entries, native identifiers, paths from native data, or user text.
        print(str(error) if type(error) is Refusal else "stopped-" + type(error).__name__, file=sys.stderr)
        sys.exit(2)
