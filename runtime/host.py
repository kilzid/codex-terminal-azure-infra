#!/usr/bin/env python3
"""Root-side transactional host reconciler. Output is a bounded, sanitized protocol.

Invoked through Azure Managed Run Command; never invoke with shell tracing.
No operation copies, restores or deletes workspace contents.
"""
import base64
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from urllib.parse import urlsplit

ROOT = Path('/etc/codex-terminal')
RECORDS = ROOT / 'revisions'
FILES = frozenset(['compose.json', 'oauth2-proxy.cfg', 'Caddyfile', 'seccomp.json',
                   'apparmor.profile', 'network-rules.sh', 'boot-guards.sh', 'codex-config.py',
                   'docker-daemon.json', 'sysctl.conf', 'unattended-upgrades.conf', 'terminal.service'])
OS_FILES = {'docker-daemon.json': '/etc/docker/daemon.json',
            'sysctl.conf': '/etc/sysctl.d/90-codex-terminal.conf',
            'unattended-upgrades.conf': '/etc/apt/apt.conf.d/52codex-security-updates',
            'terminal.service': '/etc/systemd/system/codex-terminal.service'}


def target(name):
    return Path(OS_FILES[name]) if name in OS_FILES else ROOT / name
SERVICES = ('terminal', 'auth', 'https')
PHASE = 'protocol'


def run(argv, *, data=None, timeout=600):
    result = subprocess.run(argv, input=data, capture_output=True, timeout=timeout)
    if result.returncode:
        global PHASE
        match = re.search(rb'NATIVE_PROBE_FAILED:([a-z-]+):', result.stdout)
        if match and match[1].decode() in ['http', 'origin', 'create-session', 'websocket', 'attach', 'command']:
            PHASE = 'native-' + match[1].decode()
        raise RuntimeError('host-operation-failed')
    return result.stdout


def canonical(name, data):
    if name in ('compose.json', 'seccomp.json', 'docker-daemon.json'):
        return json.dumps(json.loads(data), sort_keys=True, separators=(',', ':')).encode()
    if name == 'oauth2-proxy.cfg':
        value = tomllib.loads(data.decode())
        # OAuth2 Proxy defaults pass_host_header to true; preserve an equivalent
        # configuration during migration from the original renderer.
        value.setdefault('pass_host_header', True)
        return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    return data


def digest(name, data):
    return hashlib.sha256(canonical(name, data)).hexdigest()


def atomic(path, data, *, uid=0, gid=0, mode=0o600):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        os.fchmod(fd, mode)
        os.fchown(fd, uid, gid)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def write(name, data):
    atomic(target(name), data, uid=65532 if name == 'oauth2-proxy.cfg' else 0,
           gid=65532 if name == 'oauth2-proxy.cfg' else 0,
           mode=0o400 if name == 'oauth2-proxy.cfg' else (0o644 if name in OS_FILES else 0o600))


def compose(*args, data=None, timeout=600):
    return run(['docker', 'compose', '-f', str(ROOT / 'compose.json'), *args], data=data, timeout=timeout)


def inspect():
    ids = compose('ps', '-aq').decode().split()
    values = json.loads(run(['docker', 'inspect', *ids])) if ids else []
    return {v['Config']['Labels'].get('com.docker.compose.service'): v for v in values
            if v['Config']['Labels'].get('com.docker.compose.service') in SERVICES}


def live_hashes():
    return {name: digest(name, target(name).read_bytes()) for name in FILES if target(name).exists()}



def service_drift(files):
    if not (ROOT / 'compose.json').exists(): return []
    values = inspect()
    desired = json.loads(files['compose.json'])['services']
    drift = []
    for service, expected in desired.items():
        value = values.get(service)
        try:
            assert value and value['State']['Running'] and not value['State']['Restarting']
            config, limits = value['Config'], value['HostConfig']
            assert config['Image'] == expected['image']
            image_config = json.loads(run(['docker', 'image', 'inspect', expected['image']]))[0]['Config']
            assert config['User'] == expected.get('user', image_config.get('User', ''))
            assert limits['CapDrop'] == ['ALL'] and not limits['Privileged']
            assert 'no-new-privileges:true' in limits['SecurityOpt']
            expected_mounts = {(v.split(':')[0], v.split(':')[1], not v.endswith(':ro')) for v in expected['volumes']}
            assert {(v['Source'], v['Destination'], v['RW']) for v in value['Mounts'] if v['Type'] == 'bind'} == expected_mounts
            assert all(v['Type'] in ['bind', 'tmpfs'] for v in value['Mounts'])
            expected_tmpfs = {v.split(':', 1)[0] for v in expected.get('tmpfs', [])}
            assert set(limits.get('Tmpfs') or {}) == expected_tmpfs
            if service == 'terminal':
                assert value['AppArmorProfile'] == 'codex-terminal'
                assert any(v.startswith('seccomp=') and 'unconfined' not in v for v in limits['SecurityOpt'])
                assert limits['PortBindings']['7681/tcp'] == [{'HostIp': '127.0.0.1', 'HostPort': '7681'}]
                scale = 1024**2 if expected['mem_limit'].endswith('m') else 1024**3
                assert limits['Memory'] == int(expected['mem_limit'][:-1]) * scale
                assert limits['MemorySwap'] == limits['Memory']
                assert limits['NanoCpus'] == int(expected['cpus'] * 1e9)
                assert limits['PidsLimit'] == expected['pids_limit']
                image_env = json.loads(run(['docker', 'image', 'inspect', expected['image']]))[0]['Config'].get('Env', [])
                assert set(config.get('Env', [])) == set(image_env)
            else:
                assert limits['NetworkMode'] == 'host' and limits['ReadonlyRootfs']
                assert {v.removeprefix('CAP_') for v in limits['CapAdd'] or []} == ({'NET_BIND_SERVICE'} if service == 'https' else set())
        except (AssertionError, KeyError, TypeError): drift.append(service)
    return sorted(drift)


def changes(files):
    changed = sorted(name for name, data in files.items()
                     if not target(name).exists() or digest(name, data) != digest(name, target(name).read_bytes()))
    affected = set()
    terminal_image_only = False
    if 'compose.json' in changed:
        before = json.loads((ROOT / 'compose.json').read_bytes()) if (ROOT / 'compose.json').exists() else {'services': {}}
        after = json.loads(files['compose.json'])
        affected.update(s for s in SERVICES if before['services'].get(s) != after['services'].get(s))
        if changed == ['compose.json'] and affected == {'terminal'} and 'terminal' in before['services']:
            # Compare the complete Compose document after replacing just the image.
            # Limits, mounts, credentials and other service changes still need a backup.
            before['services']['terminal']['image'] = after['services']['terminal']['image']
            terminal_image_only = before == after
    if 'docker-daemon.json' in changed: affected.update(SERVICES)
    if 'oauth2-proxy.cfg' in changed: affected.add('auth')
    if 'Caddyfile' in changed: affected.add('https')
    if {'seccomp.json', 'apparmor.profile', 'codex-config.py'} & set(changed): affected.add('terminal')
    # Docker detects neither changed bind-mounted file contents nor profile changes.
    drift = service_drift(files)
    affected.update(drift)
    return {'changed': changed, 'affected': sorted(affected), 'serviceDrift': drift, 'before': live_hashes(),
            'terminalImageOnly': terminal_image_only and affected == {'terminal'}}


def boundaries(expected):
    values = inspect()
    assert set(values) == set(SERVICES)
    for service, item in values.items():
        assert item['State']['Running'] and not item['State']['Restarting']
        assert item['Config']['Image'] == expected['services'][service]['image']
        assert not item['HostConfig']['Privileged']
        assert item['HostConfig']['CapDrop'] == ['ALL']
        assert 'no-new-privileges:true' in item['HostConfig']['SecurityOpt']
    terminal = values['terminal']
    assert terminal['Config']['User'] == '1000:1000'
    assert terminal['AppArmorProfile'] == 'codex-terminal'
    assert any(v.startswith('seccomp=') and 'unconfined' not in v for v in terminal['HostConfig']['SecurityOpt'])
    assert terminal['HostConfig']['PortBindings']['7681/tcp'] == [{'HostIp': '127.0.0.1', 'HostPort': '7681'}]
    assert {m['Source'] for m in terminal['Mounts']} == {'/srv/codex-terminal/workspace', '/srv/codex-terminal/codex-config'}
    assert values['auth']['HostConfig']['ReadonlyRootfs']
    assert values['https']['HostConfig']['ReadonlyRootfs']
    assert {v.removeprefix('CAP_') for v in values['https']['HostConfig']['CapAdd'] or []} == {'NET_BIND_SERVICE'}
    proxy = tomllib.loads((ROOT / 'oauth2-proxy.cfg').read_text())
    assert proxy['provider'] == 'oidc' and proxy['oidc_groups_claim'] == 'oid'
    assert proxy['cookie_secure'] and proxy['cookie_httponly'] and not proxy['insecure_oidc_skip_nonce']
    assert proxy['code_challenge_method'] == 'S256'
    assert all(not proxy[key] for key in ['pass_access_token', 'pass_authorization_header', 'set_authorization_header',
                                        'pass_basic_auth', 'pass_user_headers'])
    for address, ports in [('169.254.169.254/32', [None]), ('168.63.129.16/32', ['80', '32526'])]:
        for port in ports:
            rule = ['iptables', '-C', 'DOCKER-USER', '-d', address]
            if port: rule += ['-p', 'tcp', '--dport', port]
            run([*rule, '-j', 'REJECT'])
    return values


def verify(payload, *, deep=True):
    global PHASE
    PHASE = 'verify-services'
    expected = json.loads(base64.b64decode(payload['files']['compose.json']))
    assert not service_drift({k: base64.b64decode(v) for k, v in payload['files'].items()})
    values = boundaries(expected)
    PHASE = 'verify-identity'
    proxy = tomllib.loads((ROOT / 'oauth2-proxy.cfg').read_text())
    identity = payload['identity']
    assert proxy['allowed_groups'] == [identity['owner']]
    assert proxy['client_id'] == identity['client']
    assert proxy['oidc_issuer_url'] == 'https://login.microsoftonline.com/' + identity['tenant'] + '/v2.0'
    assert proxy['redirect_url'] == 'https://' + identity['host'] + '/oauth2/callback'
    PHASE = 'verify-metadata'
    compose('exec', '-T', 'terminal', 'sh', '-ceu',
            'test ! -S /var/run/docker.sock; test ! -e /etc/oauth2-proxy.cfg; '
            'if curl -fsS --max-time 3 -o /dev/null -H Metadata:true '
            '"http://169.254.169.254/metadata/instance?api-version=2021-02-01"; then exit 1; fi')
    if deep:
        PHASE = 'verify-sandbox'
        sandbox = compose('exec', '-T', 'terminal', 'sh', data=base64.b64decode(payload['sandbox']), timeout=90)
        assert b'INNER_SANDBOX_WORKSPACE_WRITE_OK' in sandbox and b'INNER_SANDBOX_WRITE_DENIED' in sandbox
        PHASE = 'verify-native'
        native = compose('exec', '-T', '-e', 'PROBE_HOST=' + identity['host'], 'terminal',
                         'node', '--input-type=module', '-', data=base64.b64decode(payload['nativeProbe']), timeout=90)
        assert any(marker in native for marker in [b'DEPLOYED_TERMBEAM_HTTP_WEBSOCKET_ORIGIN_OK',
                                                  b'DEPLOYED_REMOBI_HTTP_WEBSOCKET_ORIGIN_PTY_OK'])
    return {name: hashlib.sha256(v['Id'].encode()).hexdigest() for name, v in values.items()}


def initialize_host():
    # Fresh VM readiness is established by cloud-init; adoption never calls this.
    run(['cloud-init', 'status', '--wait'])
    assert (ROOT / 'os-ready').exists()
    for directory in ['workspace', 'codex-config']:
        path = Path('/srv/codex-terminal') / directory
        path.mkdir(mode=0o700, exist_ok=True, parents=True)
        os.chown(path, 1000, 1000)


def record(payload, extra=None):
    previous = json.loads((ROOT / 'applied.json').read_text()) if (ROOT / 'applied.json').exists() else {}
    value = {'commit': payload['commit'], 'fingerprint': payload['fingerprint'],
             'images': payload['images'], 'hashes': live_hashes(),
             'recordedAt': dt.datetime.now(dt.timezone.utc).isoformat(),
             **({'rollbackRevision': previous['rollbackRevision']} if previous.get('rollbackRevision') else {}), **(extra or {})}
    if all(previous.get(k) == v for k, v in value.items() if k != 'recordedAt'): return previous
    atomic(ROOT / 'applied.json', json.dumps(value, sort_keys=True).encode())
    return value



def configure_codex_defaults():
    compose('stop', '--timeout', '30', 'terminal')
    config_root = Path('/srv/codex-terminal/codex-config')
    for entry in [config_root / 'config.toml', config_root / 'sqlite']:
        assert not entry.is_symlink()
        if entry.is_file(): assert entry.stat().st_nlink == 1
    run(['python3', str(ROOT / 'codex-config.py')])


def verify_restored(payload):
    previous = tomllib.loads((ROOT / 'oauth2-proxy.cfg').read_text())
    old_payload = {**payload, 'files': {n: base64.b64encode(target(n).read_bytes()).decode() for n in FILES},
        'identity': {'tenant': urlsplit(previous['oidc_issuer_url']).path.split('/')[-2],
                     'client': previous['client_id'], 'owner': previous['allowed_groups'][0],
                     'host': urlsplit(previous['redirect_url']).hostname}}
    deadline = time.monotonic() + 120
    while True:
        try:
            verify(old_payload)
            return previous
        except (AssertionError, RuntimeError):
            if time.monotonic() >= deadline: raise
            time.sleep(3)


def restore(revision, *, preserve_active_credentials=False):
    manifest = json.loads((revision / 'manifest.json').read_text())
    active_proxy = tomllib.loads((ROOT / 'oauth2-proxy.cfg').read_text()) if preserve_active_credentials else None
    for name in manifest['changed']:
        previous = revision / name
        if previous.exists():
            data = previous.read_bytes()
            if name == 'oauth2-proxy.cfg' and active_proxy:
                text = data.decode()
                for key in ['client_secret', 'cookie_secret']:
                    text = re.sub(r'^' + key + r'\s*=.*$', lambda _: key + ' = ' + json.dumps(active_proxy[key]), text, flags=re.M)
                data = text.encode()
            write(name, data)
        else: target(name).unlink(missing_ok=True)
    if (revision / 'applied.json').exists(): atomic(ROOT / 'applied.json', (revision / 'applied.json').read_bytes())
    if {'apparmor.profile', 'boot-guards.sh', 'network-rules.sh'} & set(manifest['changed']):
        run(['bash', str(ROOT / 'boot-guards.sh')])
    if 'codex-config.py' in manifest['changed']: configure_codex_defaults()
    if 'terminal.service' in manifest['changed']: run(['systemctl', 'daemon-reload'])
    if 'sysctl.conf' in manifest['changed']: run(['sysctl', '--system'])
    if 'docker-daemon.json' in manifest['changed']: run(['systemctl', 'restart', 'docker'])
    for service in manifest['affected']:
        compose('up', '-d', '--no-deps', '--pull', 'never', '--force-recreate', service)
    return manifest


def apply(payload, files):
    global PHASE
    PHASE = 'plan'
    plan = changes(files)
    assert plan['before'] == payload['expectedBefore'], 'concurrent-host-change'
    assert not (ROOT / 'pending.json').exists(), 'unfinished-transaction-requires-recovery'
    if not plan['changed'] and not plan['serviceDrift']:
        verify(payload, deep=False)
        record(payload)
        return {'changed': [], 'affected': [], 'status': 'unchanged'}
    fresh = not (ROOT / 'compose.json').exists()
    if fresh:
        PHASE = 'os-readiness'
        initialize_host()
    before_ids = {s: v['Id'] for s, v in inspect().items()} if not fresh else {}
    previous_auth = Path('/srv/codex-terminal/codex-config/auth.json')
    auth_hash = hashlib.sha256(previous_auth.read_bytes()).digest() if previous_auth.exists() else None
    PHASE = 'pull-images'
    for service in plan['affected']:
        image = json.loads(files['compose.json'])['services'][service]['image']
        run(['docker', 'pull', image])
    revision_id = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + str(time.time_ns())
    revision = RECORDS / revision_id
    revision.mkdir(mode=0o700, parents=True)
    for name in plan['changed']:
        if target(name).exists(): atomic(revision / name, target(name).read_bytes())
    if (ROOT / 'applied.json').exists(): atomic(revision / 'applied.json', (ROOT / 'applied.json').read_bytes())
    atomic(revision / 'manifest.json', json.dumps(plan).encode())
    atomic(ROOT / 'pending.json', json.dumps({'revision': revision_id}).encode())
    settled = False
    try:
        PHASE = 'install-configuration'
        for name in plan['changed']: write(name, files[name])
        compose('config', '--quiet')
        if 'terminal.service' in plan['changed']:
            run(['systemctl', 'daemon-reload'])
            run(['systemctl', 'enable', 'codex-terminal.service'])
        if 'sysctl.conf' in plan['changed']: run(['sysctl', '--system'])
        if 'docker-daemon.json' in plan['changed']: run(['systemctl', 'restart', 'docker'])
        if fresh or {'apparmor.profile', 'boot-guards.sh', 'network-rules.sh'} & set(plan['changed']):
            run(['bash', str(ROOT / 'boot-guards.sh')])
        if fresh or 'codex-config.py' in plan['changed']: configure_codex_defaults()
        if 'https' in plan['affected']:
            compose('run', '--rm', '--no-deps', 'https', 'caddy', 'validate',
                    '--config', '/etc/caddy/Caddyfile', '--adapter', 'caddyfile')
        PHASE = 'start-services'
        for service in plan['affected']:
            compose('up', '-d', '--no-deps', '--pull', 'never', '--force-recreate', service)
        PHASE = 'verify-runtime'
        deadline = time.monotonic() + 120
        while True:
            try:
                verify(payload)
                break
            except (AssertionError, RuntimeError):
                if time.monotonic() >= deadline: raise
                time.sleep(3)
        after = inspect()
        assert all(after[s]['Id'] == identifier for s, identifier in before_ids.items() if s not in plan['affected'])
        assert (hashlib.sha256(previous_auth.read_bytes()).digest() if previous_auth.exists() else None) == auth_hash
        if fresh: run(['systemctl', 'start', 'codex-terminal.service'])
        record(payload, {'rollbackRevision': revision_id})
        # Mark success before the controller checks public TLS. Its failure invokes rollback.
        atomic(revision / 'completed', b'1')
        settled = True
        return {'changed': plan['changed'], 'affected': plan['affected'], 'status': 'applied', 'revision': revision_id}
    except Exception:
        # Fresh-install failures have no previous runnable configuration.
        if not fresh:
            restore(revision)
            verify_restored(payload)
        settled = True
        raise RuntimeError('runtime-update-failed-previous-configuration-restored' if not fresh else 'fresh-runtime-failed') from None
    finally:
        if settled: (ROOT / 'pending.json').unlink(missing_ok=True)


def main(payload):
    global PHASE
    PHASE = payload.get('action', 'protocol')
    ROOT.mkdir(mode=0o700, exist_ok=True)
    files = {k: base64.b64decode(v, validate=True) for k, v in payload.get('files', {}).items()}
    assert set(files) == FILES
    action = payload['action']
    if action == 'plan':
        result = changes(files)
        applied = json.loads((ROOT / 'applied.json').read_text()) if (ROOT / 'applied.json').exists() else None
        result['applied'] = {k: applied[k] for k in ['commit', 'fingerprint', 'rollbackRevision'] if k in applied} if applied else None
        return result
    if action == 'verify':
        result = changes(files)
        assert not result['changed'] and not result['serviceDrift'], 'host-configuration-drift'
        ids = verify(payload)
        applied = json.loads((ROOT / 'applied.json').read_text())
        assert applied['fingerprint'] == payload['fingerprint'], 'configuration-record-drift'
        return {'status': 'verified', 'containers': ids}
    if action == 'adopt':
        plan = changes(files)
        # An absent helper is inert management tooling. Install it without
        # executing it only after checking the already configured user defaults.
        additions = [n for n in plan['changed'] if n == 'codex-config.py' and n not in plan['before']]
        assert set(plan['changed']) == set(additions) and not plan['serviceDrift'], 'adoption-must-have-no-functional-changes'
        defaults = tomllib.loads(Path('/srv/codex-terminal/codex-config/config.toml').read_text())
        assert all(defaults.get(k) == v for k, v in {'sqlite_home': '/home/node/.codex/sqlite',
            'sandbox_mode': 'workspace-write', 'approval_policy': 'on-request', 'cli_auth_credentials_store': 'file'}.items())
        before = verify(payload, deep=False)
        for name in additions: write(name, files[name])
        record(payload, {'adopted': True})
        assert before == verify(payload, deep=False)
        return {'status': 'adopted', 'containers': before, 'changed': [], 'affected': [], 'managementFilesAdded': additions}
    if action == 'apply': return apply(payload, files)
    if action == 'rollback':
        revision_id = payload.get('revision')
        if not revision_id and (ROOT / 'pending.json').exists():
            revision_id = json.loads((ROOT / 'pending.json').read_text())['revision']
        if not revision_id:
            revision_id = json.loads((ROOT / 'applied.json').read_text()).get('rollbackRevision')
        assert isinstance(revision_id, str) and re.fullmatch(r'[0-9T]+-[0-9]+', revision_id)
        revision = RECORDS / revision_id
        manifest = restore(revision, preserve_active_credentials=not payload.get('automaticRollback', False))
        previous = verify_restored(payload)
        (ROOT / 'pending.json').unlink(missing_ok=True)
        return {'status': 'rolled-back', 'affected': manifest['affected'],
                'hostname': urlsplit(previous['redirect_url']).hostname, 'callback': previous['redirect_url']}
    if action == 'commit':
        # Keep encrypted-at-rest old proxy configurations only for the configured
        # revision count. Docker images are not pruned: rollback must work offline.
        revisions = sorted(p for p in RECORDS.glob('*') if p.is_dir())
        for old in revisions[:-payload['runtimeRevisions']]: shutil.rmtree(old)
        cutoff = time.time() - payload.get('runCommandDays', 7) * 86400
        for old in (ROOT / 'run-command-archive').glob('*.json'):
            if old.stat().st_mtime < cutoff: old.unlink()
        return {'status': 'recorded'}
    raise ValueError('unsupported-operation')


if __name__ == '__main__':
    try:
        os.umask(0o077)
        payload = json.loads(base64.b64decode(os.environ.pop('CODEX_VM_PAYLOAD'), validate=True))
        with open('/run/codex-terminal-deployment.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = main(payload)
        print('CODEX_RESULT=' + base64.b64encode(json.dumps(result, separators=(',', ':')).encode()).decode())
    except Exception as error:
        # Never print exceptions, subprocess output, local filenames, config or logs.
        print('CODEX_OPERATION_FAILED:' + PHASE + ':' + type(error).__name__, file=sys.stderr)
        sys.exit(1)
