#!/usr/bin/env python3
"""Materialize environment secrets in an ephemeral, private runner directory."""
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'operations'))
from cloud import atomic, require
from controller import validate

p = validate(json.loads(os.environ.pop('PRIVATE_CONFIG')), 'private')
s = validate(json.loads(os.environ.pop('RUNTIME_SECRETS')), 'runtime-secrets')
require(os.environ['GITHUB_REPOSITORY_ID'] == str(p['repositoryId']), 'ci-repository-rejected')
require(os.environ['GITHUB_REPOSITORY_OWNER_ID'] == str(p['repositoryOwnerId']), 'ci-owner-rejected')
require(os.environ['GITHUB_REF'] == 'refs/heads/main', 'ci-branch-rejected')
for environment, key in [('AZURE_CLIENT_ID', 'deploymentClientId'), ('AZURE_TENANT_ID', 'tenantId'), ('AZURE_SUBSCRIPTION_ID', 'subscriptionId')]:
    require(os.environ.pop(environment) == p[key], 'ci-login-input-mismatch')
def mask(value):
    if isinstance(value, dict):
        for item in value.values(): mask(item)
    elif isinstance(value, list):
        for item in value: mask(item)
    elif isinstance(value, (str, int)) and not isinstance(value, bool):
        masked = str(value).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
        print('::add-mask::' + masked)
mask(p)
mask(s)
path = Path(os.environ['RUNNER_TEMP']) / 'codex-private'
path.mkdir(mode=0o700, exist_ok=True)
atomic(path / 'private.json', p)
atomic(path / 'runtime-secrets.json', s)
