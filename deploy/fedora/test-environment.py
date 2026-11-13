#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Manage only the disposable Thoughts Test services, database, and credentials."""
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import stat
import subprocess
import sys
import time
import urllib.request

ROOT = Path('/srv/thoughts-test')
ACCOUNT = 'thoughts-test'
DATABASE = 'thoughts_test'
SERVICE = 'thoughts-test-api.service'
WORKER = 'thoughts-test-worker.service'
SERVICES = (SERVICE, WORKER)
# The test API is published privately with Tailscale Serve on this port.
HTTPS_PORT = 8443
# The operator (the account that runs this script with sudo) stages server
# revisions in ~/thoughts-test-stage/src/thoughts_server; `update` copies them
# into the test runtime only. Production is never read or written by this
# script except to copy its runtime and comment helper during install.
STAGE_RELATIVE = Path('thoughts-test-stage/src/thoughts_server')
# The Mac, like production, uses one device for Sync and another for its
# source publisher; each numbers its own operations.
DEVICES = ('thoughts-android-test', 'thoughts-mac-test', 'thoughts-mac-source-test')
# Installed from deploy/fedora/restart-services.py. It starts the API before
# the workers, so they never race to create the empty schema after a reset.
RESTART = Path('/usr/local/sbin/thoughts-restart-services')


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def operator():
    """The account that ran sudo: it stages revisions and may control the test services."""
    name = os.environ.get('SUDO_USER', '')
    if not name or name == 'root':
        raise SystemExit('Run this with sudo from the operator account')
    return pwd.getpwnam(name)


def stage(account):
    return Path(account.pw_dir) / STAGE_RELATIVE


def url():
    """The test API's private address: this host's tailnet name with the Serve port."""
    credential_file = ROOT / 'client.json'
    if credential_file.exists():
        recorded = json.loads(credential_file.read_text()).get('base_url')
        if recorded:
            return recorded
    try:
        state = json.loads(subprocess.check_output(['tailscale', 'status', '--json'], text=True, stderr=subprocess.DEVNULL))
        name = state['Self']['DNSName'].rstrip('.')
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError, TypeError, AttributeError):
        return ''
    return f'https://{name}:{HTTPS_PORT}' if name else ''


def services(action):
    """Stop (workers first) or start (API first) every test service."""
    if RESTART.is_file():
        run(str(RESTART), '--instance', 'test', '--' + action)
    else:
        run('systemctl', action, *SERVICES)


def sql(statement):
    return subprocess.check_output(['runuser', '-u', 'postgres', '--', 'psql', '-X', '-qAt', '-d', DATABASE, '-c', statement], text=True).strip()


def ready():
    for _ in range(60):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8081/v1/ready', timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError('Thoughts Test did not become ready')


def status():
    ready()
    result = json.loads(sql("SELECT json_build_object('database',current_database(),'operations',(SELECT count(*) FROM operations),'entities',(SELECT count(*) FROM entities),'devices',(SELECT json_agg(id) FROM devices));"))
    result['environment'] = 'test'
    result['url'] = url()
    result['services'] = {name: subprocess.run(['systemctl', 'is-active', name], capture_output=True, text=True).stdout.strip() for name in SERVICES}
    print(json.dumps(result))


def reset():
    # Fixed namespace and owner checks precede every destructive operation.
    assert ROOT.resolve() == ROOT and ROOT != Path('/srv/thoughts')
    assert sql("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=current_database()") == ACCOUNT
    services('stop')
    sql('DROP SCHEMA public CASCADE; CREATE SCHEMA public AUTHORIZATION "thoughts-test";')
    user = pwd.getpwnam(ACCOUNT)
    for name in ('objects', 'state', 'worktree'):
        path = ROOT / name
        assert path.resolve().parent == ROOT
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(mode=0o700)
        os.chown(path, user.pw_uid, user.pw_gid)
    services('start')
    status()


def _site_packages():
    return [path for path in (ROOT / 'venv').rglob('thoughts_server')
            if path.is_dir() and path.parent.name == 'site-packages']


def _install_source(source):
    for target in [ROOT / 'app/src/thoughts_server', *_site_packages()]:
        assert target.resolve().is_relative_to(ROOT)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target, symlinks=True)


def update():
    """Install a staged server revision into the test runtime and restart it."""
    account = operator()
    owner = account.pw_uid
    staged = stage(account)
    if not staged.is_dir() or staged.is_symlink():
        raise SystemExit(f'Stage the server package at {staged}')
    # Only plain files and directories owned by the operator are accepted, so a
    # link cannot copy another file into the test runtime.
    for path in [staged, *staged.rglob('*')]:
        info = path.lstat()
        if info.st_uid != owner or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise SystemExit(f'Refusing staged path: {path}')
    services('stop')
    _install_source(staged)
    services('start')
    status()


def install():
    account = operator()
    staged = stage(account)
    try:
        user = pwd.getpwnam(ACCOUNT)
    except KeyError:
        run('useradd', '--system', '--home-dir', str(ROOT), '--shell', '/usr/sbin/nologin', ACCOUNT)
        user = pwd.getpwnam(ACCOUNT)
    ROOT.mkdir(mode=0o750, exist_ok=True)
    os.chown(ROOT, 0, user.pw_gid)
    for name in SERVICES:
        subprocess.run(['systemctl', 'stop', name], check=False)
    # The copied runtime avoids granting the test account access to production.
    # Refresh it so the test uses the same dependencies as production.
    if (ROOT / 'venv').exists():
        shutil.rmtree(ROOT / 'venv')
    shutil.copytree('/srv/thoughts/venv', ROOT / 'venv', symlinks=True)
    (ROOT / 'bin').mkdir(mode=0o755, exist_ok=True)
    helper = Path('/srv/thoughts/bin/thoughts-comment-sync')
    if helper.exists():
        shutil.copy2(helper, ROOT / 'bin/thoughts-comment-sync')
    for name in ('objects', 'state', 'worktree'):
        path = ROOT / name
        path.mkdir(mode=0o700, exist_ok=True)
        os.chown(path, user.pw_uid, user.pw_gid)
    _install_source(staged if staged.is_dir() else ROOT / 'app/src/thoughts_server')
    exists = subprocess.check_output(['runuser', '-u', 'postgres', '--', 'psql', '-XAtc', "SELECT 1 FROM pg_roles WHERE rolname='thoughts-test'"], text=True).strip()
    if not exists:
        run('runuser', '-u', 'postgres', '--', 'psql', '-Xc', 'CREATE ROLE "thoughts-test" LOGIN;')
    exists = subprocess.check_output(['runuser', '-u', 'postgres', '--', 'psql', '-XAtc', "SELECT 1 FROM pg_database WHERE datname='thoughts_test'"], text=True).strip()
    if not exists:
        run('runuser', '-u', 'postgres', '--', 'createdb', '-O', ACCOUNT, DATABASE)
    credential_file = ROOT / 'client.json'
    credentials = json.loads(credential_file.read_text()) if credential_file.exists() else {}
    tokens = dict(credentials.get('devices', {}))
    if credentials.get('device_id') and credentials.get('token'):
        tokens.setdefault(credentials['device_id'], credentials['token'])
    for device in DEVICES:
        tokens.setdefault(device, secrets.token_urlsafe(48))
    # device_id/token keep the existing Android test wrapper working.
    base_url = credentials.get('base_url') or url()
    credentials = {'base_url': base_url, 'device_id': DEVICES[0], 'token': tokens[DEVICES[0]], 'devices': tokens}
    credential_file.write_text(json.dumps(credentials))
    credential_file.chmod(0o600)
    env = {
        'THOUGHTS_DATABASE_URL': 'postgresql+psycopg:///thoughts_test',
        'THOUGHTS_STATE_ROOT': str(ROOT / 'state'),
        'THOUGHTS_SOURCE_BUNDLE_OUTPUT': str(ROOT / 'state/source-bundle'),
        'THOUGHTS_OBJECT_ROOT': str(ROOT / 'objects'),
        'THOUGHTS_WORKTREE_ROOT': str(ROOT / 'worktree'),
        'THOUGHTS_DOCS_ROOT': str(ROOT / 'worktree'),
        'THOUGHTS_API_HOST': '127.0.0.1', 'THOUGHTS_API_PORT': '8081',
        'THOUGHTS_AUTO_CREATE_SCHEMA': 'true',
        'THOUGHTS_ALLOW_DEVELOPMENT_BUILDS': 'false',
        # Real builds, so rebuilt documents regenerate Project readings as in production.
        'THOUGHTS_RENDERER_BACKEND': 'native',
        'THOUGHTS_WORKER_POLL_SECONDS': '1',
        'THOUGHTS_BUILD_TIMEOUT_SECONDS': '1800',
        'THOUGHTS_TOOLCHAIN_KEY': 'fedora-test',
        'THOUGHTS_COMMENT_SYNC_BIN': str(ROOT / 'bin/thoughts-comment-sync'),
        'THOUGHTS_BOOTSTRAP_TOKENS': json.dumps(tokens),
    }
    # systemd needs quoting around the JSON-valued environment assignment.
    config_root = Path('/etc/thoughts-test')
    config_root.mkdir(mode=0o700, exist_ok=True)
    env_path = config_root / 'server.env'
    env_path.write_text('\n'.join(k + '=' + "'" + v + "'" for k, v in env.items()) + '\n')
    env_path.chmod(0o600)
    hardening = f'''UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
InaccessiblePaths=/srv/thoughts /etc/thoughts
ReadWritePaths={ROOT}/state {ROOT}/objects {ROOT}/worktree'''
    units = {
        SERVICE: ('Thoughts Test isolated API', 'server_main', 'After=network-online.target postgresql.service'),
        WORKER: ('Thoughts Test isolated build worker', 'worker_main', f'After=network-online.target postgresql.service {SERVICE}'),
    }
    for name, (description, entry, after) in units.items():
        Path('/etc/systemd/system/' + name).write_text(f'''[Unit]
Description={description}
{after}
Requires=postgresql.service
[Service]
User={ACCOUNT}
Group={ACCOUNT}
WorkingDirectory={ROOT}/app
EnvironmentFile=/etc/thoughts-test/server.env
Environment=XDG_DATA_HOME={ROOT}/state/xdg-data
ExecStart={ROOT}/venv/bin/python -c "from thoughts_server.cli import {entry}; {entry}()"
Restart=on-failure
RestartSec=3
{hardening}
[Install]
WantedBy=multi-user.target
''')
    destination = Path('/usr/local/sbin/thoughts-test-environment')
    if Path(__file__).resolve() != destination:
        shutil.copy2(__file__, destination)
    destination.chmod(0o755)
    helper = Path(__file__).resolve().with_name('restart-services.py')
    if helper.is_file():
        shutil.copy2(helper, RESTART)
        RESTART.chmod(0o755)
    sudoers = Path('/etc/sudoers.d/thoughts-test-environment')
    commands = ', '.join(f'{destination} {action}' for action in ('reset', 'status', 'credentials', 'update'))
    sudoers.write_text(f'{account.pw_name} ALL=(root) NOPASSWD: {commands}\n')
    sudoers.chmod(0o440)
    run('visudo', '-cf', str(sudoers))
    run('restorecon', '-RF', '/etc/thoughts-test', str(destination), str(sudoers), *([str(RESTART)] if RESTART.is_file() else []), *('/etc/systemd/system/' + name for name in SERVICES))
    run('systemctl', 'daemon-reload')
    for name in SERVICES:
        # A unit that never ran (the new worker) is not loaded and has no
        # failed state to clear; that is not an error.
        subprocess.run(['systemctl', 'reset-failed', name], check=False)
        run('systemctl', 'enable', name)
    services('stop')
    services('start')
    ready()
    run('tailscale', 'serve', '--bg', f'--https={HTTPS_PORT}', 'http://127.0.0.1:8081')
    status()


if __name__ == '__main__':
    if os.geteuid() != 0 or len(sys.argv) != 2:
        raise SystemExit('Run as root with install, update, reset, status, or credentials')
    action = sys.argv[1]
    if action == 'credentials':
        print((ROOT / 'client.json').read_text())
    elif action == 'install':
        install()
    elif action == 'update':
        update()
    elif action == 'reset':
        reset()
    elif action == 'status':
        status()
    else:
        raise SystemExit('Unknown action')
