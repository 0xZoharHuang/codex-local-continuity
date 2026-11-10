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
FILES = ('sync.py','projects.py','history.py','Watcher.swift','manage.py')


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
    ensure(set(manifest)==set(FILES)|{'Watcher'} and
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
        ensure(store.policy() == policy, 'Enrollment differs. Uninstall and archive private state before changing scope.')
    elif old or os.path.lexists(store.baseline) or any(store.backups.glob('*.json')):
        ensure(sync.legacy_policy(policy), 'Legacy installation requires --profiles all --include-history.')
    store.update('plan', policy)
    current={n:digest(base/n) for n in FILES}
    if old and has_policy and all(old[k]==v for k,v in current.items()):
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
                ensure(not (DATA/'pending.json').exists(), 'A pending transaction must finish before upgrading.')
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


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('profiles','install','status','preview','restore','uninstall'))
    parser.add_argument('--profiles', metavar='existing|all|LABEL[,LABEL...]',
                        help='Existing (or current) freezes all currently verified profiles. All includes future profiles.')
    parser.add_argument('--include-history', action='store_true',
                        help='Also include eligible local composer history; default: project organization only.')
    args=parser.parse_args()
    cmd=args.action
    if cmd not in ('install','preview') and (args.profiles is not None or args.include_history):
        parser.error('Enrollment options apply only to install and preview.')
    if cmd=='install' and args.profiles is None:
        parser.error('Install requires explicit --profiles selection.')
    if args.include_history and args.profiles is None:
        parser.error('--include-history requires an explicit --profiles selection.')
    sync.require_default_home()
    os.umask(0o077)
    if cmd=='profiles':
        print(json.dumps(sync.Store().list_profiles()))
        return
    if cmd=='preview':
        store = sync.Store()
        policy = store.select(args.profiles, args.include_history)
        print(json.dumps(store.update('plan', policy), ensure_ascii=False))
        return
    if cmd=='install':
        recognized_app()
        policy = sync.Store().select(args.profiles, args.include_history)
        for p in (ROOT,DATA,DATA/'backups'):
            p.mkdir(mode=0o700,exist_ok=True)
            check_dir(p)
        with locked('admin.lock'):
            install(policy)
        return
    if cmd=='status':
        svc=check_service()
        print('Exit listener: '+('running' if svc.returncode==0 and 'state = running' in svc.stdout else 'stopped'))
        p=DATA/'status.json'
        if p.is_file():
            own_file(p)
            report=json.loads(p.read_bytes())
            print('Last result:',report.get('state','unknown'))
            if 'time' in report:
                print('Recorded at:',time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(report['time'])))
            c=report.get('counts',{})
            if 'profiles' in c:
                print('Profiles:',c['profiles'],' Projects:',c.get('projects',{}).get('projects',0),
                      ' Project conflicts:',c.get('projects',{}).get('conflicts',0),
                      ' Recall entries copied:',c.get('history',{}).get('entries_copied',0))
        else:
            print('Waiting for the first offline sync.')
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
        ensure(any((DATA/'backups').glob('*.json')), 'There is no recovery point yet. The listener is still in its previous state.')
        with locked():
            stop()
            PLIST.unlink(missing_ok=True)
        subprocess.run([PYTHON,'-I','-B',str(src),'restore'],check=True)
        print('Listener stopped. The most recent sync was restored.')
        return
    if cmd=='uninstall':
        with locked():
            ensure(not (DATA/'pending.json').exists(), 'An unfinished transaction needs recovery after the desktop app exits.')
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
