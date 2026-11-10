#!/usr/bin/env python3
"""Fail closed on files outside the release whitelist and private data in history.

Never print matching text, filenames from rejected input, or raw git object data.
Use --private locally for exact matching against account values, never in PR CI.
"""
import argparse
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
# Repository owner explicitly permits this address in source and commit metadata.
ALLOWED_EMAILS = {'noreply@github.com', 'kilzid@gmail.com'}
ALLOWED_UUIDS = {'00000003-0000-0000-c000-000000000000', '18a4783c-866b-4cc7-a460-3d5e5662c884',
                 '246dd0d5-5bd0-4def-940b-0421030a5b68', '00000000-0000-0000-0000-000000000000'}
EXACT = {'README.md', 'LICENSE', 'NOTICE', '.gitignore', 'requirements.txt', 'images.lock.json',
         'codex-infra', 'SECURITY.md', 'RELEASE_FILES.json'}
DANGEROUS = [r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
             r'\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b',
             r'eyJ[A-Za-z0-9_-]{15,}\.eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{20,}',
             r'(?i)(?:client_secret|cookie_secret|accountkey|password)\s*[=:]\s*[\"\'][^\"\'\s]{16,}[\"\']']


def git(*args): return subprocess.check_output(['git', '-C', str(ROOT), *args])


def private_values(directory):
    if not directory: return []
    values = set()
    def visit(value, key=''):
        if isinstance(value, dict):
            for k, v in value.items(): visit(v, k)
        elif isinstance(value, list):
            for v in value: visit(v, key)
        elif isinstance(value, str) and len(value) >= 5 and key not in {
            'repository', 'profile', 'location', 'vmSize', 'budgetStartDate', 'expiresAt'}:
            # Match personal IDs, addresses, domains, names and credential material.
            if key in {'subscriptionId','tenantId','adminObjectId','userObjectId','runtimeApplicationObjectId',
                       'runtimeServicePrincipalId','runtimeClientId','deploymentApplicationObjectId','deploymentObjectId',
                       'deploymentClientId','contactEmail','hostname','azureHostname','adminPublicKey','publicIp',
                       'recoveryStorageName','auditWorkspaceName','clientSecret','cookieSecret','keyId','vmInstanceId','osDiskId','osDiskUniqueId','publicIpId'}: values.add(value)
    for name in ['private.json', 'runtime-secrets.json']:
        path = Path(directory) / name
        if path.exists(): visit(json.loads(path.read_text()))
    return sorted(values)


def scan(data, private):
    text = data.decode('utf-8', errors='strict')
    if any(v in text for v in private): raise ValueError('personal-input-found')
    if any(re.search(pattern, text) for pattern in DANGEROUS): raise ValueError('credential-pattern-found')
    for value in re.findall(r'\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b', text):
        if value.lower() not in ALLOWED_UUIDS: raise ValueError('account-identifier-found')
    for value in re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', text):
        try: address = ipaddress.ip_address(value)
        except ValueError: continue
        # Policy constants and documentation networks are explicitly public.
        if not (address.is_private or address.is_loopback or address.is_link_local or
                value in ['168.63.129.16']): raise ValueError('public-address-found')
    for value in re.findall(r'\b[A-Za-z0-9_.+%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', text):
        if value not in ALLOWED_EMAILS and not value.endswith(('@example.com', '@example.invalid', '@users.noreply.github.com')):
            raise ValueError('personal-email-found')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--private')
    args = parser.parse_args()
    private = private_values(args.private)
    release = set(json.loads((ROOT / 'RELEASE_FILES.json').read_text()))
    tracked = set(git('ls-files').decode().splitlines())
    files = set(git('ls-files', '--cached', '--others', '--exclude-standard').decode().splitlines())
    if files != release: raise ValueError('release-whitelist-mismatch')
    for name in files:
        path = ROOT / name
        if path.is_symlink() or not path.is_file(): raise ValueError('non-regular-release-file')
        scan(path.read_bytes(), private)
    seen = set()
    for line in git('rev-list', '--objects', '--all').decode().splitlines():
        oid, _, name = line.partition(' ')
        kind = git('cat-file', '-t', oid).decode().strip()
        if kind == 'blob':
            if name not in release: raise ValueError('historic-file-outside-whitelist')
            if oid not in seen: scan(git('cat-file', 'blob', oid), private)
        elif kind == 'commit': scan(git('cat-file', 'commit', oid), private)
        seen.add(oid)
    print(json.dumps({'status': 'privacy-passed', 'files': len(files), 'historyObjects': len(seen)}))


if __name__ == '__main__':
    try: main()
    except Exception:
        print('{"status":"privacy-failed","code":"private-data-or-release-whitelist-violation"}', file=sys.stderr)
        sys.exit(1)
