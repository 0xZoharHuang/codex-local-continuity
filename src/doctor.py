"""Compare local plugin and Scheduled metadata across a manual account switch."""
import argparse
from collections import Counter
from contextlib import closing
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import tempfile
import native_layout


CODEX = Path.home() / ".codex"
DATA = Path.home() / "Library/Application Support/CodexLocalContinuity/data"
BASELINE = "doctor-baseline.json"
ID_PART = r"[A-Za-z0-9_-][A-Za-z0-9._-]*"
PLUGIN_ID = re.compile(rf"({ID_PART})@({ID_PART})")
PLUGIN_HEADER = re.compile(rf"""\[\s*plugins\s*\.\s*(["'])({ID_PART}@{ID_PART})\1\s*\]\s*(?:#.*)?""")
ROOT_TOKEN = re.compile(r"""(?P<prefix>\[*\s*)(?P<key>[A-Za-z0-9_-]+|'[^']*'|"(?:[^"\\]|\\.)*")""")
CONFIG_FIELDS = ("kind", "rrule", "execution_environment", "notification_policy")
TARGET_FIELDS = ("target_type", "target_thread_id", "project_id", "cwds", "remote_host")
BINDING_FIELDS = ("user_id", "account_id", "installation_id")
SCHEDULE_COLUMNS = ("id", "status") + CONFIG_FIELDS + TARGET_FIELDS + BINDING_FIELDS
INSPECTION_ERRORS = (OSError, ValueError, TypeError, KeyError, sqlite3.Error, UnicodeError)


def digest(key, *values):
    raw = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hmac.new(key, raw, hashlib.sha256).hexdigest()


def check_regular(path, private=False):
    item = path.lstat()
    if not stat.S_ISREG(item.st_mode):
        raise ValueError("unsupported-file-layout")
    if private and (item.st_uid != os.getuid() or item.st_mode & 0o077 or item.st_nlink != 1):
        raise ValueError("unsafe-baseline-permissions")


def identity(codex, key):
    path = codex / ".codex-global-state.json"
    check_regular(path)
    with path.open(encoding="utf-8") as f:
        state = json.load(f)
    layout = native_layout.inspect(state)
    owner = layout['active']
    if owner is None:
        return {"state":"no-active-profile"}, None, None, None
    atoms = layout['owners'][owner]
    user = atoms["local-document-identity"]["userId"]
    account = atoms["local-document-identity"]["accountId"]
    if not all(isinstance(s, str) and s for s in (user, account)):
        raise ValueError("unknown-identity")
    data = json.dumps([user, account], ensure_ascii=False, separators=(",", ":")).encode()
    if hashlib.sha256(data).hexdigest() != owner:
        raise ValueError("identity-in-transition")
    cached = atoms.get("heartbeat-thread-permissions-by-id")
    return ({"state":"verified","native_layout":layout['kind']}, digest(key, "owner", owner), (user, account),
            set(cached) if isinstance(cached, dict) else None)


def plugin_settings(path):
    check_regular(path)
    result = {}
    current = None
    in_root = True
    with path.open(encoding="utf-8") as file:
        for raw in file:
            line = raw.strip()
            if line.startswith("#"):
                continue
            if '"""' in line or "'''" in line:
                # System Python 3.9 does not include a TOML parser. Do not
                # interpret a section-looking line from a multiline string.
                raise ValueError("unsupported-plugin-config")
            # TOML permits quoted table and assignment keys. Normalize the
            # first key, so unsupported plugin declarations cannot look empty.
            token = ROOT_TOKEN.match(line)
            if token:
                raw_key = token["key"]
                if raw_key.startswith('"'):
                    first = json.loads(raw_key)
                elif raw_key.startswith("'"):
                    first = raw_key[1:-1]
                else:
                    first = raw_key
                if first in ("plugins", "enabled"):
                    line = token["prefix"] + first + line[token.end():]
            if line.startswith("["):
                in_root = False
                current = None
                match = PLUGIN_HEADER.fullmatch(line)
                if match:
                    current = match[2]
                    if current in result:
                        raise ValueError("duplicate-plugin-setting")
                    result[current] = None
                elif re.match(r"\[+\s*plugins\s*[.\]]", line):
                    raise ValueError("unsupported-plugin-config")
            elif in_root and re.match(r"plugins\s*[.=]", line):
                raise ValueError("unsupported-plugin-config")
            elif current is not None and re.match(r"enabled\s*=", line):
                match = re.fullmatch(r"enabled\s*=\s*(true|false)\s*(?:#.*)?", line)
                if not match or result[current] is not None:
                    raise ValueError("unsupported-plugin-config")
                result[current] = match[1] == "true"
    return result


def cache_versions(root, name, market):
    package = root / market / name
    if any(p.is_symlink() for p in (root, package.parent, package)):
        raise ValueError("unsupported-cache-layout")
    versions = []
    if not package.exists():
        return versions
    for version in package.iterdir():
        if version.is_symlink():
            # The native cache can have a "current" alias next to the real
            # version directory. Count each physical version once.
            if version.resolve().parent != package.resolve() or not version.is_dir():
                raise ValueError("unsupported-cache-layout")
            continue
        if not version.is_dir():
            continue
        for manifest in ("plugin.json", ".codex-plugin/plugin.json", ".claude-plugin/plugin.json"):
            candidate = version / manifest
            if not candidate.parent.is_symlink() and candidate.is_file() and not candidate.is_symlink():
                versions.append(version.name)
                break
    return sorted(versions)


def plugins(codex, key):
    settings = plugin_settings(codex / "config.toml")
    summary = {"state":"ok", "configured":len(settings),
               "explicitly_enabled":sum(v is True for v in settings.values()),
               "explicitly_disabled":sum(v is False for v in settings.values()),
               "enablement_unspecified":sum(v is None for v in settings.values()),
               "with_cached_manifest":0, "without_cached_manifest":0}
    rows = {}
    for full, enabled in settings.items():
        name, market = PLUGIN_ID.fullmatch(full).groups()
        versions = cache_versions(codex / "plugins/cache", name, market)
        summary["with_cached_manifest" if versions else "without_cached_manifest"] += 1
        rows[digest(key,"plugin",full)] = {"enablement":digest(key,enabled), "cache_versions":digest(key,versions)}
    return summary, rows


def db_read(path):
    check_regular(path)
    wal, shm = [path.with_name(path.name + suffix) for suffix in ("-wal","-shm")]
    for sidecar in (wal, shm):
        if os.path.lexists(sidecar):
            check_regular(sidecar)
    paired = wal.exists() and shm.exists()
    if wal.exists() and wal.stat().st_size and not paired:
        raise ValueError("unindexed-wal")
    db = sqlite3.connect(path.as_uri() + ("?mode=ro" if paired else "?mode=ro&immutable=1"),
                         uri=True, timeout=2)
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        return db
    except Exception:
        db.close()
        raise


def definition_files(codex):
    root = codex / "automations"
    if root.is_symlink():
        raise ValueError("unsafe-directory")
    if not root.exists():
        return set()
    return {d.name for d in root.iterdir() if not d.is_symlink() and d.is_dir()
            and not (d/"automation.toml").is_symlink() and (d/"automation.toml").is_file()}


def indexed_threads(codex, targets):
    versions = {f.name for f in codex.glob("state_*.sqlite") if re.fullmatch(r"state_[0-9]+\.sqlite",f.name)}
    if not versions or max(int(n[6:-7]) for n in versions) != 5:
        return None
    try:
        with closing(db_read(codex / "state_5.sqlite")) as db:
            return {tid for tid in targets if db.execute("SELECT 1 FROM threads WHERE id=?", (tid,)).fetchone()}
    except INSPECTION_ERRORS:
        return None


def schedules(codex, key, active, permission_keys):
    files = definition_files(codex)
    file_check = {"file_inventory":sorted(digest(key,"definition-file",n) for n in files)}
    try:
        with closing(db_read(codex / "sqlite/codex.db")) as db:
            columns = [c[1] for c in db.execute("PRAGMA table_info(automations)")]
            if not set(SCHEDULE_COLUMNS).issubset(columns):
                return ({"state":"unknown", "reason":"unsupported-registry-schema", "definition_files":len(files)},
                        None, file_check)
            query = "SELECT " + ",".join(SCHEDULE_COLUMNS) + " FROM automations"
            entries = [dict(zip(SCHEDULE_COLUMNS,row)) for row in db.execute(query)]
    except INSPECTION_ERRORS as error:
        return dict(unknown(error), definition_files=len(files)), None, file_check
    local_targets = {r["target_thread_id"] for r in entries if r["target_thread_id"] and not r["remote_host"]}
    indexed = indexed_threads(codex, local_targets)
    status, binding = Counter(), Counter()
    rows = {}
    ids = set()
    for r in entries:
        rid = r["id"]
        if not isinstance(rid, str) or not rid or rid in ids:
            raise ValueError("unsupported-registry-id")
        ids.add(rid)
        status[r["status"] if r["status"] in ("ACTIVE", "PAUSED") else "OTHER"] += 1
        pair = (r["user_id"], r["account_id"])
        if not any(pair):
            category = "unbound"
        elif not all(pair) or active is None:
            category = "unknown"
        else:
            category = "active_profile" if pair == active else "other_profile"
        binding[category] += 1
        rows[digest(key,"schedule",rid)] = {
            "definition_metadata":digest(key,*[r[k] for k in CONFIG_FIELDS]),
            "target":digest(key,*[r[k] for k in TARGET_FIELDS]),
            "status":digest(key,r["status"]), "binding":digest(key,*[r[k] for k in BINDING_FIELDS]),
            "definition_file":digest(key,rid in files),
        }
    summary = {"state":"ok", "registered":len(rows), "active":status["ACTIVE"], "paused":status["PAUSED"],
               "other_status":status["OTHER"], "definition_files":len(files),
               "registry_without_file":len(ids-files), "files_without_registry":len(files-ids),
               "owner_binding":{k:binding[k] for k in ("active_profile","other_profile","unbound","unknown")},
               "rows_with_project_target":sum(bool(r["project_id"]) for r in entries),
               "rows_with_remote_host":sum(bool(r["remote_host"]) for r in entries),
               "rows_bound_to_installation":sum(bool(r["installation_id"]) for r in entries),
               "distinct_local_thread_targets":len(local_targets),
               "local_thread_target_index":"ok" if indexed is not None else "unknown",
               "indexed_targets":None if indexed is None else len(indexed),
               "missing_targets":None if indexed is None else len(local_targets-indexed),
               "targets_with_active_profile_permission_cache":
                   None if permission_keys is None else len(local_targets & permission_keys)}
    checks = {**file_check,
              "indexed_targets": None if indexed is None else sorted(digest(key,"target",t) for t in indexed),
              "permission_cache_targets": None if permission_keys is None else
                  sorted(digest(key,"target",t) for t in local_targets & permission_keys)}
    return summary, rows, checks


def unknown(error):
    return {"state":"unknown", "reason":"file-absent" if isinstance(error,FileNotFoundError)
            else "unavailable-or-unrecognized"}


def observed_identity(codex, key):
    try:
        return identity(codex,key)
    except INSPECTION_ERRORS as error:
        return unknown(error), None, None, None


def snapshot(codex, key):
    account, owner, active, perms = observed_identity(codex,key)
    report = {"profile":account}
    data = {"owner":owner}
    try:
        report["plugins"], data["plugins"] = plugins(codex,key)
    except INSPECTION_ERRORS as error:
        report["plugins"], data["plugins"] = unknown(error), None
    try:
        report["schedules"], data["schedules"], data["schedule_checks"] = schedules(codex,key,active,perms)
    except INSPECTION_ERRORS as error:
        report["schedules"], data["schedules"], data["schedule_checks"] = unknown(error), None, {}
    final_account, final_owner, _, final_perms = observed_identity(codex,key)
    transition = (account, owner) != (final_account, final_owner)
    if transition:
        report["profile"] = {"state":"unknown", "reason":"identity-changed-during-snapshot"}
        data["owner"] = None
        if report["schedules"]["state"] == "ok":
            bindings = report["schedules"]["owner_binding"]
            bindings["unknown"] = report["schedules"]["registered"] - bindings["unbound"]
            bindings["active_profile"] = bindings["other_profile"] = None
    if transition or perms != final_perms:
        if report["schedules"]["state"] == "ok":
            report["schedules"]["targets_with_active_profile_permission_cache"] = None
        data["schedule_checks"]["permission_cache_targets"] = None
    return report, data


def differences(before, after):
    if before is None or after is None:
        return {"state":"unknown"}
    shared = before.keys() & after.keys()
    changes = Counter()
    for rid in shared:
        for field in before[rid]:
            if before[rid][field] != after[rid][field]:
                changes[field] += 1
    return {"state":"compared", "added":len(after.keys()-before.keys()), "removed":len(before.keys()-after.keys()),
            "changed":sum(before[rid]!=after[rid] for rid in shared), "changed_by_field":dict(changes)}


def private_dirs(data, create=False):
    if data.resolve() != data:
        raise ValueError("unsafe-baseline-directory")
    for directory in (data.parent, data):
        if create:
            directory.mkdir(mode=0o700, exist_ok=True)
        item = directory.lstat()
        if not stat.S_ISDIR(item.st_mode) or item.st_uid != os.getuid() or item.st_mode & 0o077:
            raise ValueError("unsafe-baseline-directory")


def write_baseline(file, obj, replace):
    private_dirs(file.parent, create=True)
    if os.path.lexists(file):
        check_regular(file, private=True)
        if not replace:
            raise FileExistsError("baseline-exists")
    fd, temporary = tempfile.mkstemp(dir=file.parent, prefix=".doctor-")
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(obj,out,separators=(",",":"))
            out.flush()
            os.fsync(out.fileno())
        if replace:
            os.replace(temporary,file)
        else:
            os.link(temporary,file)
    finally:
        Path(temporary).unlink(missing_ok=True)


def run(mode="show", *, data=DATA, codex=CODEX, replace_baseline=False):
    if mode not in ("show","baseline","compare") or replace_baseline and mode != "baseline":
        raise ValueError("invalid-doctor-mode")
    data, codex = Path(data).absolute(), Path(codex).absolute()
    file = data / BASELINE
    key = secrets.token_bytes(32)
    previous = None
    if mode == "compare":
        private_dirs(data)
        check_regular(file, private=True)
        with file.open(encoding="utf-8") as f:
            saved = json.load(f)
        if saved["version"] != 1 or not re.fullmatch(r"[0-9a-f]{64}",saved["key"]):
            raise ValueError("unsupported-baseline")
        previous, key = saved["before"], bytes.fromhex(saved["key"])
    report, current = snapshot(codex,key)
    report["source"] = "local-disk"
    report["not_observed"] = ["plugin-ui-visibility","external-service-authorization",
                              "scheduled-ui-visibility","cloud-registry","task-execution"]
    if mode == "baseline":
        write_baseline(file, {"version":1,"key":key.hex(),"before":current}, replace_baseline)
        report["baseline"] = "saved"
    if previous is not None:
        report["comparison"] = {"active_profile":"unknown" if not previous["owner"] or not current["owner"]
                                else "same" if previous["owner"] == current["owner"] else "different"}
        for family in ("plugins","schedules"):
            report["comparison"][family] = differences(previous[family],current[family])
        checks = {}
        for k in ("file_inventory","indexed_targets","permission_cache_targets"):
            old, new = previous.get("schedule_checks",{}).get(k), current["schedule_checks"].get(k)
            checks[k] = ({"state":"unknown"} if old is None or new is None else
                         {"state":"compared","added":len(set(new)-set(old)),"removed":len(set(old)-set(new))})
        report["comparison"]["schedule_checks"] = checks
    return report


def format_report(report, lang="en"):
    zh = lang in ("zh","zh-CN")
    unknown_text = "未知" if zh else "unknown"
    def shown(number):
        return str(number) if number is not None else unknown_text
    lines = []
    comparison = report.get("comparison")
    if comparison:
        relation = comparison["active_profile"]
        lines.append(("与基线相比，当前账户及工作区身份" +
                      {"same":"相同","different":"不同","unknown":"无法核对"}[relation]) if zh else
                     f"Current account/workspace compared with baseline — {relation}")
    else:
        valid = report["profile"]["state"] == "verified"
        lines.append(("当前本机身份已核实" if valid else "当前本机身份未核实") if zh else
                     ("Current local identity verified" if valid else "Current local identity unverified"))
    p = report["plugins"]
    if p["state"] == "ok":
        lines.append((f"插件配置 {p['configured']} 项，明确启用 {p['explicitly_enabled']} 项，"
                      f"找到缓存包 {p['with_cached_manifest']} 项") if zh else
                     (f"{p['configured']} plugins configured, {p['explicitly_enabled']} explicitly enabled, "
                      f"{p['with_cached_manifest']} with cached manifests"))
    else:
        lines.append("插件配置或缓存无法核对" if zh else "Plugin configuration or cache unknown")
    s = report["schedules"]
    if s["state"] == "ok":
        targets = s["distinct_local_thread_targets"]
        found = shown(s["indexed_targets"])
        lines.append((f"本机定时任务 {s['registered']} 个，启用 {s['active']}，暂停 {s['paused']}。"
                      f"本地聊天目标已找到 {found}/{targets} 个") if zh else
                     (f"{s['registered']} local schedules registered, {s['active']} active, {s['paused']} paused. "
                      f"{found}/{targets} distinct local thread targets found"))
    else:
        files = shown(s.get("definition_files"))
        lines.append(f"本机定时任务登记状态未知，定义文件 {files} 个" if zh else
                     f"Local schedule registry unknown. Definition file count is {files}.")
    if comparison:
        labels = {"enablement":"启用设置","cache_versions":"缓存版本","status":"状态","target":"目标",
                  "definition_metadata":"定义元数据","definition_file":"定义文件","binding":"身份绑定"}
        for key, name in (("plugins","插件" if zh else "Plugins"),("schedules","定时任务" if zh else "Schedules")):
            diff = comparison[key]
            if diff["state"] != "compared":
                lines.append(name + ("差异未知" if zh else " diff unknown"))
                continue
            detail = ", ".join(f"{labels.get(k,k) if zh else k} {n}" for k,n in sorted(diff["changed_by_field"].items()))
            line = ((f"{name}新增 {diff['added']}，缺少 {diff['removed']}，变化 {diff['changed']}") if zh else
                    f"{name} — {diff['added']} added, {diff['removed']} missing, {diff['changed']} changed")
            lines.append(f"{line} ({detail})" if detail else line)
        for key, label in (("file_inventory","定义文件" if zh else "Definition files"),
                           ("indexed_targets","已找到的聊天目标" if zh else "Indexed thread targets"),
                           ("permission_cache_targets","有权限缓存的目标" if zh else "Targets with permission cache")):
            delta = comparison["schedule_checks"][key]
            if delta["state"] == "compared" and (delta["added"] or delta["removed"]):
                lines.append(f"{label} +{delta['added']} / -{delta['removed']}")
    if report.get("baseline") == "saved":
        lines.append("已保存私有本机基线" if zh else "Private local baseline saved")
    lines.append("应用里的可见性、外部授权、云端任务和实际运行情况仍需单独核实。" if zh else
                 "App visibility, external authorization, cloud schedules, and actual runs were not observed.")
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode",nargs="?",choices=("show","baseline","compare"),default="show")
    parser.add_argument("--replace-baseline",action="store_true")
    parser.add_argument("--json",action="store_true")
    parser.add_argument("--lang",choices=("en","zh","zh-CN"),default="en")
    args = parser.parse_args()
    try:
        report = run(args.mode,replace_baseline=args.replace_baseline)
        print(json.dumps(report,indent=2) if args.json else format_report(report,args.lang))
    except (OSError,ValueError,KeyError,TypeError) as error:
        reason = ("baseline-already-exists" if isinstance(error,FileExistsError) else
                  "baseline-missing" if isinstance(error,FileNotFoundError) else "invalid-request-or-baseline")
        parser.exit(2,reason+"\n")
