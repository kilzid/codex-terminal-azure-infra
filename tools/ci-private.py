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
for value in [*p.values(), *p['resources'].values(), *s.values()]:
    if isinstance(value, str):
        masked = value.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
        print('::add-mask::' + masked)
path = Path(os.environ['RUNNER_TEMP']) / 'codex-private'
path.mkdir(mode=0o700, exist_ok=True)
atomic(path / 'private.json', p)
atomic(path / 'runtime-secrets.json', s)
