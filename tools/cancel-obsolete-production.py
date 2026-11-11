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


def cancel(api):
    current = api('/git/ref/heads/main')['object']['sha']
    response = api('/actions/runs?status=in_progress&per_page=100')
    u.require(response['total_count'] <= 100, 'too-many-active-runs')
    candidates = [r for r in response['workflow_runs'] if eligible(r, current)]
    u.require(len(candidates) <= 1, 'multiple-obsolete-production-runs')
    if not candidates: return {'status': 'no-obsolete-production-run'}
    run = candidates[0]
    u.require(api('/compare/' + run['head_sha'] + '...' + current)['status'] == 'ahead', 'obsolete-run-not-main-ancestor')
    latest = api('/actions/runs/' + str(run['id']))
    u.require(eligible(latest, current) and latest['head_sha'] == run['head_sha'], 'run-changed-before-cancel')
    u.require(api('/git/ref/heads/main')['object']['sha'] == current, 'main-changed-before-cancel')
    api('/actions/runs/' + str(run['id']) + '/cancel', 'POST')
    return {'status': 'obsolete-production-cancellation-requested', 'run': run['id']}


if __name__ == '__main__':
    try:
        u.require(os.environ.get('GITHUB_REF') == 'refs/heads/main' and
                  os.environ.get('GITHUB_EVENT_NAME') in ['push', 'workflow_dispatch'], 'trusted-main-run-required')
        print(json.dumps(cancel(u.GitHub(os.environ['GITHUB_REPOSITORY'], os.environ['GH_TOKEN']))))
    except u.UpdateError as error:
        print(json.dumps({'status': 'cancellation-failed', 'code': str(error)})); sys.exit(1)
    except Exception:
        print(json.dumps({'status': 'cancellation-failed', 'code': 'details-withheld'})); sys.exit(1)
