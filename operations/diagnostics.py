"""Read-only, bounded Azure operation summaries. Never output resource names or messages."""
import json
import re
from pathlib import Path
import sys
import urllib.error
from cloud import Cloud, Failure, require
from controller import load_json

STATES = {'Accepted', 'Running', 'Succeeded', 'Failed', 'Canceled', 'Cancelled', 'InProgress',
          'NotFound', 'Pending', 'Completed', 'CompletedWithWarnings', 'NotStarted', 'Creating', 'Updating', 'Deleting'}


def state(value):
    return value if value in STATES else 'Unknown'


def errors(value):
    """Retain machine error codes only; messages can contain private resource identifiers."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ['code', 'errorCode'] and (type(item) is int or
                    isinstance(item, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9]{0,99}', item)):
                found.append(item)
            elif key in ['error', 'details', 'errorDetails', 'statusMessage']:
                if isinstance(item, str):
                    try: item = json.loads(item)
                    except ValueError: continue
                found.extend(errors(item))
    elif isinstance(value, list):
        for item in value: found.extend(errors(item))
    return found[:20]


def timestamp(value):
    return value if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}T[0-9:.]+Z', value) else None


def diagnose(cloud):
    vault = cloud.rg + '/providers/Microsoft.RecoveryServices/vaults/' + cloud.private['resources']['vaultName']
    jobs = cloud.arm(vault + '/backupJobs', api='2025-02-01').get('value', [])
    jobs = sorted(jobs, key=lambda v: v.get('properties', {}).get('startTime', ''), reverse=True)[:5]
    backup = []
    for job in jobs:
        # Details are a GET, and can explain a failed or long-running snapshot.
        identifier = job.get('id', '')
        require(identifier.lower().startswith(vault.lower() + '/backupjobs/'), 'diagnostics-scope-rejected')
        p = cloud.arm(identifier, api='2025-02-01')['properties']
        backup.append({'status': state(p.get('status')), 'startedAt': timestamp(p.get('startTime')), 'endedAt': timestamp(p.get('endTime')),
                       'errorCodes': errors(p)})
    deployment = cloud.rg + '/providers/Microsoft.Resources/deployments/codex-infra'
    try:
        p = cloud.arm(deployment, api='2025-04-01').get('properties', {})
        operations = cloud.arm(deployment + '/operations', api='2025-04-01').get('value', [])
    except urllib.error.HTTPError as error:
        if error.code != 404: raise
        p, operations = {'provisioningState': 'NotFound'}, []
    summary = []
    for operation in operations:
        op = operation.get('properties', {})
        if op.get('provisioningState') != 'Succeeded':
            summary.append({'status': state(op.get('provisioningState')), 'errorCodes': errors(op)})
    return {'backupJobs': backup, 'deployment': {'status': state(p.get('provisioningState')),
            'errorCodes': errors(p), 'unfinishedOperations': summary[:20]}}


if __name__ == '__main__':
    try:
        cloud = Cloud(load_json(Path(sys.argv[1]) / 'private.json', 'private'))
        cloud.refresh_workload_login(force=True)
        cloud.preflight()
        print(json.dumps({'status': 'diagnostics-complete', 'identityRefresh': 'verified', **diagnose(cloud)}))
    except Failure as error:
        print(json.dumps({'status': 'diagnostics-failed', 'code': str(error)})); sys.exit(1)
    except Exception:
        print(json.dumps({'status': 'diagnostics-failed', 'code': 'azure-read-failed'})); sys.exit(1)
