#!/usr/bin/python3
"""Install or manage the local Codex exit listener for this macOS user."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync

ROOT = Path.home() / 'Library/Application Support/CodexLocalContinuity'
DATA = ROOT/'data'
BIN = ROOT/'bin'
LABEL = 'local.codex.continuity'
DOMAIN = f'gui/{os.getuid()}'
PLIST = Path.home()/'Library/LaunchAgents'/(LABEL+'.plist')
PYTHON = '/usr/bin/python3'
OLD_FILES = ('sync.py','projects.py','history.py','Watcher.swift','manage.py')
PRIOR_FILES = OLD_FILES + ('capabilities.py','doctor.py','settings_ui.py')
FILES = PRIOR_FILES + ('native_layout.py','modern_sync.py','recall.py')


def ensure(ok, message):
    if not ok:
        raise RuntimeError(message)


def check_dir(p):
    st=p.lstat()
    ensure(p.resolve()==p and stat.S_ISDIR(st.st_mode) and st.st_uid==os.getuid()
           and not (st.st_mode & 0o077), 'Unsafe helper directory or permissions.')


def own_file(p):
    st=p.lstat()
    ensure(stat.S_ISREG(st.st_mode) and st.st_uid==os.getuid() and st.st_nlink==1
           and not (st.st_mode & 0o022), 'Unsafe helper file or permissions.')


def digest(p):
    own_file(p)
    return hashlib.sha256(p.read_bytes()).hexdigest()


def set_file(p, content):
    fd, temp=tempfile.mkstemp(dir=p.parent)
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp,p)
    finally:
        Path(temp).unlink(missing_ok=True)


def definition():
    return {'Label':LABEL,'ProgramArguments':[str(BIN/'Watcher')],
            'RunAtLoad':True,'KeepAlive':{'SuccessfulExit':False},'LimitLoadToSessionType':'Aqua',
            'ProcessType':'Background','ThrottleInterval':10}


def recognized_app(roots=None):
    choices = []
    for root in roots if roots is not None else (Path('/Applications'), Path.home()/'Applications'):
        for name in ('Codex.app', 'ChatGPT.app'):
            app = root/name
            info = app/'Contents/Info.plist'
            if not info.is_file() or app.is_symlink():
                continue
            try:
                metadata = plistlib.loads(info.read_bytes())
            except (OSError, ValueError, plistlib.InvalidFileException):
                continue
            binary = metadata.get('CFBundleExecutable')
            if metadata.get('CFBundleIdentifier') != 'com.openai.codex' or binary not in ('Codex','ChatGPT'):
                continue
            exe = app/'Contents/MacOS'/binary
            if exe.is_file() and os.access(exe, os.X_OK):
                choices.append(app.resolve())
    ensure(len(set(choices)) == 1, 'Exactly one recognized Codex desktop app is required for installation.')


def service():
    return subprocess.run(['/bin/launchctl','print',DOMAIN+'/'+LABEL],
                          stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)


def check_service():
    if os.path.lexists(PLIST):
        own_file(PLIST)
        ensure(plistlib.loads(PLIST.read_bytes())==definition(), 'Another LaunchAgent uses this label.')
    result=service()
    if result.returncode==0:
        ensure(PLIST.exists() and str(BIN/'Watcher') in result.stdout, 'The running job does not match this installation.')
    return result


def check_bin():
    check_dir(BIN)
    own_file(BIN/'manifest.json')
    manifest=json.loads((BIN/'manifest.json').read_bytes())
    ensure(set(manifest) in (set(FILES)|{'Watcher'}, set(PRIOR_FILES)|{'Watcher'}, set(OLD_FILES)|{'Watcher'}) and
           {f.name for f in BIN.iterdir()}==set(manifest)|{'manifest.json'},
           'Unexpected files in the installed directory.')
    ensure(all(digest(BIN/n)==h for n,h in manifest.items()), 'An installed file failed its integrity check.')
    return manifest


@contextlib.contextmanager
def locked(name='job.lock'):
    lockfile=DATA/name
    fd=os.open(lockfile,os.O_CREAT|os.O_NOFOLLOW|os.O_RDWR,0o600)
    with os.fdopen(fd,'a') as f:
        own_file(lockfile)
        deadline=time.monotonic()+12
        while True:
            try:
                fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
                break
            except BlockingIOError:
                ensure(time.monotonic()<deadline, 'A sync transaction is still running. Try again shortly.')
                time.sleep(.1)
        yield


def stop():
    if check_service().returncode==0:
        subprocess.run(['/bin/launchctl','bootout',DOMAIN+'/'+LABEL],check=True)
        for _ in range(50):
            if service().returncode!=0:
                return
            time.sleep(.1)
        raise RuntimeError('The previous listener did not finish exiting.')


def start():
    if not os.path.lexists(PLIST):
        set_file(PLIST,plistlib.dumps(definition()))
    if check_service().returncode!=0:
        subprocess.run(['/bin/launchctl','bootstrap',DOMAIN,str(PLIST)],check=True)
    for _ in range(50):
        v=service()
        if v.returncode==0 and 'state = running' in v.stdout and 'pid = ' in v.stdout:
            return
        time.sleep(.1)
    raise RuntimeError('Could not verify that the listener is running.')


def install(policy):
    PLIST.parent.mkdir(mode=0o700,exist_ok=True)
    ensure(PLIST.parent.resolve()==PLIST.parent, 'Unexpected LaunchAgents directory.')
    check_service()
    base=Path(__file__).resolve().parent
    old=check_bin() if os.path.lexists(BIN) else None
    store = sync.Store()
    has_policy = os.path.lexists(store.enrollment)
    if has_policy:
        ensure(sync.equivalent(store.policy(), policy), 'Enrollment differs. Use configure to change sharing settings.')
    elif old or os.path.lexists(store.baseline) or any(store.backups.glob('*.json')):
        ensure(sync.legacy_policy(policy), 'Legacy installation requires --profiles all --include-history.')
    store.update('plan', policy)
    current={n:digest(base/n) for n in FILES}
    if old and has_policy and all(old.get(k)==v for k,v in current.items()):
        start()
        print('Already installed. The exit listener is running.')
        return
    stage=Path(tempfile.mkdtemp(dir=ROOT,prefix='.install-'))
    previous=ROOT/('.previous-'+uuid.uuid4().hex)
    was_running=service().returncode==0
    try:
        for name in FILES:
            set_file(stage/name,(base/name).read_bytes())
        subprocess.run(['/usr/bin/swiftc','-O',str(stage/'Watcher.swift'),'-o',str(stage/'Watcher')],check=True)
        (stage/'Watcher').chmod(0o700)
        manifest={k:digest(stage/k) for k in (*FILES,'Watcher')}
        set_file(stage/'manifest.json',json.dumps(manifest,sort_keys=True).encode())
        swapped = False
        deployment_started = False
        try:
            with locked():
                ensure(not os.path.lexists(sync.Store().pending), 'A pending transaction must finish before upgrading.')
                deployment_started = True
                if old:
                    stop()
                    os.rename(BIN,previous)
                if not has_policy:
                    sync.replace(store.enrollment, sync.packed(policy))
                os.rename(stage,BIN)
                swapped = True
            # Bootstrap after releasing the worker lock: an offline install can
            # legitimately start its first sync immediately.
            start()
        except BaseException:
            if not deployment_started:
                raise
            with locked():
                stop()
                if swapped:
                    os.rename(BIN,stage)
                if previous.exists():
                    os.rename(previous,BIN)
                if not has_policy:
                    store.enrollment.unlink(missing_ok=True)
                if not old:
                    PLIST.unlink(missing_ok=True)
            if was_running:
                start()
            raise
        if previous.exists():
            shutil.rmtree(previous)
        print('Installed. The listener checks on desktop exit or when it starts with the app closed.')
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def saved_policy(store):
    return store.policy() if os.path.lexists(store.enrollment) else None


def public_policy(policy):
    if policy is None:
        return None
    return {"profiles": policy['scope'], "labels": [o[:12] for o in policy['owners']],
            "features": sync.policy_features(policy)}


def status(store):
    svc = check_service()
    try:
        layout = sync.inspect_layout(sync.read_json(sync.read_regular(store.live)))['kind']
    except Exception:
        layout = 'unknown'
    return {"listener": "running" if svc.returncode == 0 and "state = running" in svc.stdout else "stopped",
            "policy": public_policy(saved_policy(store)),
            "last_sync": store.load(store.log, {"state": "no-sync-under-current-policy"}),
            "native_layout": layout,
            "capture": store.load(store.baseline.parent/'capture-status.json',{'state':'not-yet-captured'}),
            "recovery_points": len(list(store.backups.glob("*.json")))}


def print_status(info, lang='en'):
    zh = lang == 'zh'
    print(('退出监听器：' if zh else 'Exit listener: ') + (
        {'running':'运行中','stopped':'已停止'}[info['listener']] if zh else info['listener']))
    p = info['policy']
    if info.get('native_layout')=='shared-v2':
        print('项目由新版 App 原生共享；输入历史在保存时留存，退出后回填。' if zh else
              'Projects are shared natively. Recall is checkpointed on saved changes and restored after exit.')
        capture = info.get('capture',{})
        print('输入留存：' if zh else 'Recall checkpoint:',capture.get('state','unknown'))
        if capture.get('time'):
            print('留存时间：' if zh else 'Checkpoint at:',
                  time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(capture['time'])))
    if p:
        group = ('所有已初始化身份（含以后新增）' if zh else 'all initialized, including future'
                 ) if p['profiles']=='all' else ', '.join(p['labels'])
        print('共享模式：' if zh else 'Sharing:', p['features'], '|', group)
    else:
        print('尚未配置共享。' if zh else 'Sharing has not been configured.')
    report = info['last_sync']
    state = report.get('state','unknown')
    old_failure = (info.get('native_layout')=='shared-v2' and
                   info.get('capture',{}).get('state')=='captured' and
                   state=='unrecognized-layout-or-io-KeyError')
    if old_failure:
        print('旧版的格式错误已适配，输入留存已恢复；等待下一次退出回填。' if zh else
              'The previous format error is now supported. Recall capture is active; awaiting the next exit.')
    print('最近退出结果：' if zh else 'Last exit result:', state,
          '| 恢复点：' if zh else '| Recovery points:', info['recovery_points'])
    if 'time' in report:
        print('记录时间：' if zh else 'Recorded at:', time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(report['time'])))
    c = report.get('counts', {})
    if 'profiles' in c:
        print('身份数：' if zh else 'Profiles:', c['profiles'],
              '| 项目数：' if zh else '| Projects:', c.get('projects', {}).get('projects', 0),
              '| 分组冲突：' if zh else '| Conflicts:', c.get('projects', {}).get('conflicts', 0),
              '| 补入输入记录：' if zh else '| Recall entries copied:', c.get('history', {}).get('entries_copied', 0))


def apply_settings(store, before_token, candidate):
    check_dir(ROOT)
    check_dir(DATA)
    with locked('admin.lock'):
        manifest = check_bin()
        ensure(manifest.get("sync.py") == digest(Path(__file__).resolve().with_name("sync.py")),
               "Upgrade the installed helper before changing its policy.")
        with locked():
            ensure(digest(store.enrollment) == before_token, "The policy changed while you were reviewing it. Open settings again.")
            return store.reconfigure(candidate)


def enroll_and_install(policy):
    recognized_app()
    for p in (ROOT, DATA, DATA/'backups'):
        p.mkdir(mode=0o700, exist_ok=True)
        check_dir(p)
    with locked('admin.lock'):
        install(policy)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('profiles','install','upgrade','settings','configure','capabilities',
                                           'doctor','status','preview','restore','uninstall'))
    parser.add_argument('operation', nargs='?', choices=('show','baseline','compare'), help='doctor operation')
    parser.add_argument('--profiles', metavar='existing|all|LABEL[,LABEL...]',
                        help='Freeze current identities, include future ones, or select local profile labels.')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--features', choices=('folders','folders-history','projects','history','both','off'), help='Independent local sharing modes.')
    group.add_argument('--include-history', action='store_true', help='Compatibility alias for --features both.')
    parser.add_argument('--replace-baseline', action='store_true', help='Replace an existing doctor baseline.')
    parser.add_argument('--json', action='store_true', help='Machine-readable settings, status, capabilities or doctor report.')
    parser.add_argument('--lang', choices=('en','zh'), default='en', help='Language for the settings and capabilities views.')
    args = parser.parse_args()
    cmd = args.action
    if (args.operation or args.replace_baseline) and cmd != 'doctor':
        parser.error('Doctor options apply only to the doctor command.')
    if args.replace_baseline and args.operation != 'baseline':
        parser.error('--replace-baseline requires doctor baseline.')
    feature = 'both' if args.include_history else args.features
    if (args.profiles is not None or feature is not None) and cmd not in ('install','configure','preview'):
        parser.error('Enrollment options apply only to install, configure or preview.')
    if cmd == 'install' and args.profiles is None:
        parser.error('Install requires explicit --profiles selection.')
    if cmd == 'configure' and args.profiles is None and feature is None:
        parser.error('Choose --profiles or --features; use settings for the guided editor.')
    sync.require_default_home()
    os.umask(0o077)
    store = sync.Store()
    if cmd == 'doctor':
        import doctor
        result = doctor.run(args.operation or 'show', data=DATA, codex=sync.CODEX,
                            replace_baseline=args.replace_baseline)
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else doctor.format_report(result, args.lang))
        return
    if cmd == 'profiles':
        print(json.dumps(store.list_profiles()))
        return
    if cmd == 'capabilities':
        import capabilities
        current = saved_policy(store)
        rows = capabilities.report(sync.policy_features(current) if current else None, args.lang)
        try:
            layout = sync.inspect_layout(sync.read_json(sync.read_regular(store.live)))
        except Exception:
            layout = None
        if layout and layout['kind']=='shared-v2':
            for row in rows:
                if row['id'] in ('folders','projects'):
                    row.update(control='native',setting=None,action='Codex → sidebar (native shared projects)')
        if args.json:
            print(json.dumps(rows, ensure_ascii=False))
        else:
            labels = {'enabled':'已启用','disabled':'未启用','unconfigured':'尚未设置',
                      'local-audit':'本机检查','account':'由账号管理','native':'在 Codex 操作'}
            for row in rows:
                kind = row['setting'] or row['control']
                print(f"{row['feature']} [{labels[kind] if args.lang=='zh' else kind}]  {row['action']}")
        return
    if cmd == 'status':
        value = status(store)
        if args.json:
            print(json.dumps(value))
        else:
            print_status(value, args.lang)
        return
    if cmd == 'settings':
        before_token = digest(store.enrollment) if os.path.lexists(store.enrollment) else None
        previous = saved_policy(store)
        if args.json or not sys.stdin.isatty():
            print(json.dumps(public_policy(previous)))
            return
        import settings_ui
        will_install = previous is None or not BIN.is_dir()
        candidate = settings_ui.edit(store, lang=args.lang, will_install=will_install)
        if candidate is not None:
            if will_install:
                enroll_and_install(candidate)
            else:
                print(json.dumps(apply_settings(store, before_token, candidate)))
        return
    if cmd == 'preview':
        policy = store.propose(args.profiles, feature)
        print(json.dumps(store.update('plan', policy), ensure_ascii=False))
        return
    if cmd == 'install':
        enroll_and_install(store.propose(args.profiles, feature or 'projects'))
        return
    if cmd == 'upgrade':
        ensure(BIN.is_dir() and saved_policy(store) is not None, "Install the helper and explicitly select profiles first.")
        enroll_and_install(store.policy())
        return
    if cmd == 'configure':
        ensure(os.path.lexists(store.enrollment) and BIN.is_dir(), "Install the helper before reconfiguring it.")
        before_token = digest(store.enrollment)
        print(json.dumps(apply_settings(store, before_token, store.propose(args.profiles, feature))))
        return
    check_dir(ROOT)
    check_dir(DATA)
    with locked('admin.lock'):
        modify(cmd)


def modify(cmd):
    if cmd=='restore':
        # May also be invoked from the portable copy after an uninstall.
        src=BIN/'sync.py' if BIN.is_dir() else Path(__file__).resolve().with_name('sync.py')
        if BIN.is_dir():
            check_bin()
        sys.path.insert(0,str(src.parent))
        from sync import offline
        ensure(offline(), 'Quit the desktop app before restoring. The listener is still in its previous state.')
        ensure(any(sync.Store().backups.glob('*.json')), 'There is no recovery point yet. The listener is still in its previous state.')
        with locked():
            stop()
            PLIST.unlink(missing_ok=True)
        subprocess.run([PYTHON,'-I','-B',str(src),'restore'],check=True)
        print('Listener stopped. The most recent sync was restored.')
        return
    if cmd=='uninstall':
        with locked():
            ensure(not os.path.lexists(sync.Store().pending), 'An unfinished transaction needs recovery after the desktop app exits.')
            if os.path.lexists(BIN):
                check_bin()
            stop()
            PLIST.unlink(missing_ok=True)
            if BIN.is_dir():
                shutil.rmtree(BIN)
        print('Listener uninstalled. Native settings and private recovery points were retained.')


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        print(str(exc),file=sys.stderr)
        sys.exit(1)
