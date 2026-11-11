#!/usr/bin/env python3
"""Cancel one obsolete Production controller, without any Azure credentials."""
import importlib.util
import json
import os
from pathlib import Path
import sys
spec = importlib.util.spec_from_file_location('updater', Path(__file__).with_name('update-terminal.py'))
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)


def eligible(run, current):
    return (run.get('path') == '.github/workflows/production.yml' and run.get('name') == 'Production' and
            run.get('head_branch') == 'main' and run.get('status') == 'in_progress' and
            run.get('head_sha') != current)


def cancel(api, run_id):
    u.require(u.re.fullmatch(r'[1-9][0-9]{0,19}', str(run_id)), 'invalid-run-id')
    path = '/actions/runs/' + str(run_id)
    current = api('/git/ref/heads/main')['object']['sha']
    run = api(path)
    u.require(run.get('path') == '.github/workflows/production.yml' and run.get('name') == 'Production' and
              run.get('head_branch') == 'main', 'not-a-production-run')
    if run.get('status') == 'completed': return {'status': 'production-run-already-completed', 'run': int(run_id)}
    u.require(eligible(run, current), 'not-an-obsolete-production-run')
    u.require(api('/compare/' + run['head_sha'] + '...' + current)['status'] == 'ahead', 'obsolete-run-not-main-ancestor')
    latest = api(path)
    if latest.get('status') == 'completed': return {'status': 'production-run-already-completed', 'run': int(run_id)}
    u.require(eligible(latest, current) and latest['head_sha'] == run['head_sha'], 'run-changed-before-cancel')
    u.require(api('/git/ref/heads/main')['object']['sha'] == current, 'main-changed-before-cancel')
    try:
        api(path + '/cancel', 'POST')
    except u.UpdateError as error:
        # A manual cancellation can win the race after the status recheck.
        if str(error) != 'github-api-409' or api(path).get('status') != 'completed': raise
        return {'status': 'production-run-already-completed', 'run': int(run_id)}
    return {'status': 'obsolete-production-cancellation-requested', 'run': int(run_id)}


if __name__ == '__main__':
    try:
        u.require(os.environ.get('GITHUB_REF') == 'refs/heads/main' and
                  os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch', 'trusted-main-run-required')
        print(json.dumps(cancel(u.GitHub(os.environ['GITHUB_REPOSITORY'], os.environ['GH_TOKEN']), os.environ['TARGET_RUN_ID'])))
    except u.UpdateError as error:
        print(json.dumps({'status': 'cancellation-failed', 'code': str(error)})); sys.exit(1)
    except Exception:
        print(json.dumps({'status': 'cancellation-failed', 'code': 'details-withheld'})); sys.exit(1)
