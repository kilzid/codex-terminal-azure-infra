#!/usr/bin/env python3
"""Exercise the pinned authentication binary against a disposable local issuer."""
import base64
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
from render import proxy_config, caddy_config


def run(*args):
    result = subprocess.run(list(args), capture_output=True)
    if result.returncode: raise RuntimeError('synthetic-container-policy-test-failed')
    return result.stdout


def wait(url):
    for attempt in range(60):
        try:
            with urllib.request.urlopen(url, timeout=2): return
        except OSError: time.sleep(1)
    raise RuntimeError('synthetic-service-not-ready')


def main():
    images = json.loads((ROOT / 'images.lock.json').read_text())
    owner = '00000000-0000-0000-0000-000000000000'
    issuer_name, proxy_name = 'codex-policy-issuer', 'codex-policy-proxy'
    with tempfile.TemporaryDirectory(prefix='codex-policy-') as directory:
        path = Path(directory)
        path.chmod(0o755)
        (path / 'issuer.mjs').write_bytes((ROOT / 'runtime/test-issuer.mjs').read_bytes())
        config = proxy_config('test-tenant', 'test-client', owner, 'test.invalid', 'synthetic-test-only',
                              base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
        for before, after in [
            ('https://login.microsoftonline.com/test-tenant/v2.0', 'http://127.0.0.1:19000'),
            ('https://test.invalid/oauth2/callback', 'http://127.0.0.1:14181/oauth2/callback'),
            ('127.0.0.1:4180', '127.0.0.1:14181'), ('127.0.0.1:7681', '127.0.0.1:19000'),
            ('cookie_secure = true', 'cookie_secure = false'), ('__Host-codex_terminal', 'codex_test')]:
            config = config.replace(before, after)
        (path / 'proxy.cfg').write_text(config)
        (path / 'Caddyfile').write_text(caddy_config('terminal.example.com','terminal.example.com','owner@example.com'))
        for file in path.iterdir(): file.chmod(0o644)
        try:
            run('docker','run','-d','--name',issuer_name,'--network','host','--user','1000:1000','--cap-drop','ALL',
                '--security-opt','no-new-privileges:true','--entrypoint','node','-e','OWNER_OID='+owner,
                '-v',str(path)+':/test:ro',images['terminal'],'/test/issuer.mjs')
            wait('http://127.0.0.1:19000/keys')
            run('docker','run','-d','--name',proxy_name,'--network','host','--user','65532:65532','--cap-drop','ALL',
                '--security-opt','no-new-privileges:true','--read-only','--tmpfs','/tmp',
                '-v',str(path/'proxy.cfg')+':/etc/test.cfg:ro',images['auth'],'--config=/etc/test.cfg')
            wait('http://127.0.0.1:14181/ping')
            run(sys.executable,str(ROOT/'runtime/test-proxy-flow.py'))
            run('docker','run','--rm','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true',
                '--entrypoint','caddy','-v',str(path/'Caddyfile')+':/etc/caddy/Caddyfile:ro',images['https'],
                'validate','--config','/etc/caddy/Caddyfile','--adapter','caddyfile')
        finally:
            subprocess.run(['docker','rm','-f',proxy_name,issuer_name],capture_output=True)
    print('{"status":"proxy-issuer-audience-signature-nonce-owner-and-tls-tests-passed"}')


if __name__ == '__main__':
    try: main()
    except Exception:
        print('{"status":"synthetic-proxy-policy-test-failed"}',file=sys.stderr)
        sys.exit(1)
