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


def run(argv, *, data=None, timeout=600):
    result = subprocess.run(argv, input=data, capture_output=True, timeout=timeout)
    if result.returncode:
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


def compose(*args, data=None):
    return run(['docker', 'compose', '-f', str(ROOT / 'compose.json'), *args], data=data)


def inspect():
    ids = compose('ps', '-aq').decode().split()
    values = json.loads(run(['docker', 'inspect', *ids])) if ids else []
    return {v['Config']['Labels'].get('com.docker.compose.service'): v for v in values
            if v['Config']['Labels'].get('com.docker.compose.service') in SERVICES}


def live_hashes():
    return {name: digest(name, target(name).read_bytes()) for name in FILES if target(name).exists()}


def changes(files):
    changed = sorted(name for name, data in files.items()
                     if not target(name).exists() or digest(name, data) != digest(name, target(name).read_bytes()))
    affected = set()
    if 'compose.json' in changed:
        before = json.loads((ROOT / 'compose.json').read_bytes()) if (ROOT / 'compose.json').exists() else {'services': {}}
        after = json.loads(files['compose.json'])
        affected.update(s for s in SERVICES if before['services'].get(s) != after['services'].get(s))
    if 'docker-daemon.json' in changed: affected.update(SERVICES)
    if 'oauth2-proxy.cfg' in changed: affected.add('auth')
    if 'Caddyfile' in changed: affected.add('https')
    if {'seccomp.json', 'apparmor.profile', 'codex-config.py'} & set(changed): affected.add('terminal')
    # Docker detects neither changed bind-mounted file contents nor profile changes.
    return {'changed': changed, 'affected': sorted(affected), 'before': live_hashes()}


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
    assert set(values['https']['HostConfig']['CapAdd'] or []) == {'NET_BIND_SERVICE'}
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
    expected = json.loads(base64.b64decode(payload['files']['compose.json']))
    values = boundaries(expected)
    proxy = tomllib.loads((ROOT / 'oauth2-proxy.cfg').read_text())
    identity = payload['identity']
    assert proxy['allowed_groups'] == [identity['owner']]
    assert proxy['client_id'] == identity['client']
    assert proxy['oidc_issuer_url'] == 'https://login.microsoftonline.com/' + identity['tenant'] + '/v2.0'
    assert proxy['redirect_url'] == 'https://' + identity['host'] + '/oauth2/callback'
    compose('exec', '-T', 'terminal', 'sh', '-ceu',
            'test ! -S /var/run/docker.sock; test ! -e /etc/oauth2-proxy.cfg; '
            'if curl -fsS --max-time 3 -o /dev/null -H Metadata:true '
            '"http://169.254.169.254/metadata/instance?api-version=2021-02-01"; then exit 1; fi')
    if deep:
        sandbox = compose('exec', '-T', 'terminal', 'sh', data=base64.b64decode(payload['sandbox']))
        assert b'INNER_SANDBOX_WORKSPACE_WRITE_OK' in sandbox and b'INNER_SANDBOX_WRITE_DENIED' in sandbox
        native = compose('exec', '-T', '-e', 'PROBE_HOST=' + identity['host'], 'terminal',
                         'node', '--input-type=module', '-', data=base64.b64decode(payload['nativeProbe']))
        assert b'DEPLOYED_TERMBEAM_HTTP_WEBSOCKET_ORIGIN_OK' in native
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


def restore(revision):
    manifest = json.loads((revision / 'manifest.json').read_text())
    for name in manifest['changed']:
        previous = revision / name
        if previous.exists(): write(name, previous.read_bytes())
        else: target(name).unlink(missing_ok=True)
    if (revision / 'applied.json').exists(): atomic(ROOT / 'applied.json', (revision / 'applied.json').read_bytes())
    if {'apparmor.profile', 'boot-guards.sh', 'network-rules.sh'} & set(manifest['changed']):
        run(['bash', str(ROOT / 'boot-guards.sh')])
    if 'terminal.service' in manifest['changed']: run(['systemctl', 'daemon-reload'])
    if 'sysctl.conf' in manifest['changed']: run(['sysctl', '--system'])
    if 'docker-daemon.json' in manifest['changed']: run(['systemctl', 'restart', 'docker'])
    for service in manifest['affected']:
        compose('up', '-d', '--no-deps', '--pull', 'never', '--force-recreate', service)
    return manifest


def apply(payload, files):
    plan = changes(files)
    assert plan['before'] == payload['expectedBefore'], 'concurrent-host-change'
    assert not (ROOT / 'pending.json').exists(), 'unfinished-transaction-requires-recovery'
    if not plan['changed']:
        verify(payload, deep=False)
        record(payload)
        return {'changed': [], 'affected': [], 'status': 'unchanged'}
    fresh = not (ROOT / 'compose.json').exists()
    if fresh: initialize_host()
    before_ids = {s: v['Id'] for s, v in inspect().items()} if not fresh else {}
    previous_auth = Path('/srv/codex-terminal/codex-config/auth.json')
    auth_hash = hashlib.sha256(previous_auth.read_bytes()).digest() if previous_auth.exists() else None
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
    try:
        for name in plan['changed']: write(name, files[name])
        compose('config', '--quiet')
        if 'terminal.service' in plan['changed']:
            run(['systemctl', 'daemon-reload'])
            run(['systemctl', 'enable', 'codex-terminal.service'])
        if 'sysctl.conf' in plan['changed']: run(['sysctl', '--system'])
        if 'docker-daemon.json' in plan['changed']: run(['systemctl', 'restart', 'docker'])
        if fresh or {'apparmor.profile', 'boot-guards.sh', 'network-rules.sh'} & set(plan['changed']):
            run(['bash', str(ROOT / 'boot-guards.sh')])
        if fresh or 'codex-config.py' in plan['changed']:
            run(['python3', str(ROOT / 'codex-config.py')])
        if 'https' in plan['affected']:
            compose('run', '--rm', '--no-deps', 'https', 'caddy', 'validate',
                    '--config', '/etc/caddy/Caddyfile', '--adapter', 'caddyfile')
        for service in plan['affected']:
            compose('up', '-d', '--no-deps', '--pull', 'never', '--force-recreate', service)
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
        return {'changed': plan['changed'], 'affected': plan['affected'], 'status': 'applied', 'revision': revision_id}
    except Exception:
        # Fresh-install failures have no previous runnable configuration.
        if not fresh: restore(revision)
        raise RuntimeError('runtime-update-failed-previous-configuration-restored' if not fresh else 'fresh-runtime-failed') from None
    finally:
        (ROOT / 'pending.json').unlink(missing_ok=True)


def main(payload):
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
        assert not result['changed'], 'host-configuration-drift'
        ids = verify(payload)
        applied = json.loads((ROOT / 'applied.json').read_text())
        assert applied['fingerprint'] == payload['fingerprint'], 'configuration-record-drift'
        return {'status': 'verified', 'containers': ids}
    if action == 'adopt':
        plan = changes(files)
        assert not plan['changed'], 'adoption-must-have-no-functional-changes'
        before = verify(payload, deep=False)
        record(payload, {'adopted': True})
        assert before == verify(payload, deep=False)
        return {'status': 'adopted', 'containers': before, 'changed': [], 'affected': []}
    if action == 'apply': return apply(payload, files)
    if action == 'rollback':
        revision_id = payload.get('revision')
        if not revision_id:
            revision_id = json.loads((ROOT / 'applied.json').read_text()).get('rollbackRevision')
        assert isinstance(revision_id, str) and re.fullmatch(r'[0-9T]+-[0-9]+', revision_id)
        revision = RECORDS / revision_id
        manifest = restore(revision)
        boundaries(json.loads((ROOT / 'compose.json').read_bytes()))
        (ROOT / 'pending.json').unlink(missing_ok=True)
        return {'status': 'rolled-back', 'affected': manifest['affected']}
    if action == 'commit':
        # Keep encrypted-at-rest old proxy configurations only for the configured
        # revision count. Docker images are not pruned: rollback must work offline.
        revisions = sorted(p for p in RECORDS.glob('*') if p.is_dir())
        for old in revisions[:-payload['runtimeRevisions']]: shutil.rmtree(old)
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
    except Exception:
        # Never print exceptions, subprocess output, local filenames, config or logs.
        print('CODEX_OPERATION_FAILED', file=sys.stderr)
        sys.exit(1)
