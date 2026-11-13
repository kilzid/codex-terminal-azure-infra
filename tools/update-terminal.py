#!/usr/bin/env python3
"""Discover a public image, validate an isolated PR, merge and dispatch production.

Only main's trusted script runs with write permissions. Candidate code runs in
Validate with a read-only token and no production environment or credentials.
"""
import base64
import copy
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

IMAGE = 'ghcr.io/dorseg/codex-terminal'
DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')
PREFIX = 'automation/terminal-image-'
BOT = {'name': 'github-actions[bot]', 'email': '41898282+github-actions[bot]@users.noreply.github.com'}
REQUIRED_JOBS = {'validate', 'container-policies'}
ACTIVE = {'queued', 'in_progress', 'pending', 'waiting', 'requested'}


class UpdateError(Exception):
    """Only fixed, public error codes may leave the process."""


def require(value, code):
    if not value: raise UpdateError(code)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl): return None


def read_url(url, *, headers=None, method='GET', body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={'Content-Type': 'application/json', **(headers or {})})
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=45) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
        require(len(raw) <= 2 * 1024 * 1024, 'response-too-large')
        return raw, response.headers


def latest_digest():
    token_raw, _ = read_url('https://ghcr.io/token?scope=repository:dorseg/codex-terminal:pull')
    token = json.loads(token_raw)['token']
    raw, headers = read_url('https://ghcr.io/v2/dorseg/codex-terminal/manifests/latest', headers={
        'Authorization': 'Bearer ' + token,
        'Accept': 'application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json'})
    digest = headers.get('Docker-Content-Digest', '')
    require(DIGEST.fullmatch(digest), 'invalid-registry-digest')
    require(digest == 'sha256:' + hashlib.sha256(raw).hexdigest(), 'registry-digest-mismatch')
    manifest = json.loads(raw)
    require(manifest.get('schemaVersion') == 2 and any(
        item.get('platform') == {'architecture': 'amd64', 'os': 'linux'}
        and DIGEST.fullmatch(item.get('digest', '')) for item in manifest.get('manifests', [])),
        'linux-amd64-image-required')
    return digest


def replace_terminal(lock, digest):
    require(DIGEST.fullmatch(digest), 'invalid-image-digest')
    require(set(lock) == {'terminal', 'auth', 'https', 'os'} and
            lock['terminal'].startswith(IMAGE + '@') and
            DIGEST.fullmatch(lock['terminal'].split('@', 1)[1]), 'invalid-current-lockfile')
    result = copy.deepcopy(lock)
    result['terminal'] = IMAGE + '@' + digest
    return result


class GitHub:
    def __init__(self, repository, token):
        require(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository), 'invalid-repository')
        self.repository = repository
        self.token = token

    def __call__(self, path, method='GET', body=None):
        require(path.startswith('/') and not path.startswith('//'), 'invalid-api-path')
        try:
            raw, _ = read_url('https://api.github.com/repos/' + self.repository + path,
                headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                         'X-GitHub-Api-Version': '2022-11-28'}, method=method, body=body)
            return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            if error.code == 403 and path == '/pulls' and method == 'POST':
                raise UpdateError('actions-pull-request-creation-not-permitted') from None
            if error.code == 405 and re.fullmatch(r'/pulls/[0-9]+/merge', path) and method == 'PUT':
                raise UpdateError('protected-merge-blocked-pr-left-open') from None
            raise UpdateError('github-api-' + str(error.code)) from None

    def optional(self, path):
        try: return self(path)
        except UpdateError as error:
            if str(error) == 'github-api-404': return None
            raise

    def pages(self, path, *, key=None):
        values = []
        for page in range(1, 101):
            rows = self(path + ('&' if '?' in path else '?') + f'per_page=100&page={page}')
            if key is not None:
                require(isinstance(rows, dict) and isinstance(rows.get(key), list), 'invalid-paginated-response')
                rows = rows[key]
            require(isinstance(rows, list), 'invalid-paginated-response')
            values.extend(rows)
            if len(rows) < 100: return values
        raise UpdateError('pagination-limit-exceeded')

    def lock(self, ref):
        value = self('/contents/images.lock.json?ref=' + urllib.parse.quote(ref, safe=''))
        require(value.get('encoding') == 'base64', 'invalid-lockfile-encoding')
        return json.loads(base64.b64decode(value['content']))


def managed_pr(pr, repository):
    return (pr.get('user', {}).get('login') == BOT['name'] and
            (pr.get('head', {}).get('repo') or {}).get('full_name') == repository and
            pr.get('head', {}).get('ref', '').startswith(PREFIX) and
            pr.get('base', {}).get('ref') == 'main')


def passed_jobs(run, jobs, head):
    return (run.get('head_sha') == head and run.get('event') == 'workflow_dispatch' and
            run.get('status') == 'completed' and run.get('conclusion') == 'success' and
            REQUIRED_JOBS <= {job['name'] for job in jobs
                              if job.get('status') == 'completed' and job.get('conclusion') == 'success'})


class Updater:
    def __init__(self, github, *, sleep=time.sleep, now=time.monotonic):
        self.api, self.sleep, self.now = github, sleep, now

    def main_sha(self): return self.api('/git/ref/heads/main')['object']['sha']

    def production(self, head):
        require(self.main_sha() == head, 'main-changed-before-deployment')
        runs = self.api('/actions/workflows/production.yml/runs?head_sha=' + head + '&per_page=100')['workflow_runs']
        if any(r['head_sha'] == head and (r['status'] in ACTIVE or r.get('conclusion') == 'success') for r in runs):
            return False
        self.api('/actions/workflows/production.yml/dispatches', 'POST',
                 {'ref': 'main', 'inputs': {'reconcile': True}})
        return True

    def candidate(self, base, digest, lock):
        # The base suffix avoids rewriting branches or overwriting human commits.
        # An outdated bot PR is replaced, with at most one managed PR left open.
        branch = PREFIX + digest.split(':')[1][:16] + '-' + base[:12]
        commit = self.api('/git/commits/' + base)
        content = json.dumps(lock, indent=2) + '\n'
        blob = self.api('/git/blobs', 'POST', {'content': content, 'encoding': 'utf-8'})
        tree = self.api('/git/trees', 'POST', {'base_tree': commit['tree']['sha'], 'tree': [
            {'path': 'images.lock.json', 'mode': '100644', 'type': 'blob', 'sha': blob['sha']}]})
        ref = self.api.optional('/git/ref/heads/' + branch)
        if ref:
            head = ref['object']['sha']
            existing = self.api('/git/commits/' + head)
            require(existing['tree']['sha'] == tree['sha'] and
                    [p['sha'] for p in existing['parents']] == [base] and
                    existing['author']['email'] == BOT['email'], 'automation-branch-modified')
        else:
            head = self.api('/git/commits', 'POST', {'message': 'Update terminal image to ' + digest,
                'tree': tree['sha'], 'parents': [base], 'author': BOT, 'committer': BOT})['sha']
            self.api('/git/refs', 'POST', {'ref': 'refs/heads/' + branch, 'sha': head})
        open_prs = self.api.pages('/pulls?state=open&base=main')
        matching = [p for p in open_prs if p['head']['ref'] == branch and
                    (p['head'].get('repo') or {}).get('full_name') == self.api.repository]
        require(len(matching) <= 1, 'duplicate-image-pull-requests')
        if matching:
            pr = matching[0]
            require(managed_pr(pr, self.api.repository), 'automation-pull-request-modified')
        else:
            pr = self.api('/pulls', 'POST', {'base': 'main', 'head': branch,
                'title': 'Update Codex Terminal image',
                'body': 'Update only the terminal image to `' + IMAGE + '@' + digest + '`.\n\n'
                        'The daily image updater merges this PR only after Validate passes on its exact head. '
                        'Production then applies and verifies the VM. Daily backups cover workspace data; '
                        'deployments do not trigger or wait for an extra on-demand backup.'})
        for old in open_prs:
            if old['number'] != pr['number'] and managed_pr(old, self.api.repository):
                self.api('/pulls/' + str(old['number']), 'PATCH', {'state': 'closed'})
        return branch, head, pr['number']

    def validation_status(self, head, state, run_id=None):
        # workflow_dispatch job checks are not eligible PR merge requirements.
        # The trusted main updater attests the verified results with commit statuses;
        # candidate workflows keep a read-only token and never report their own success.
        descriptions = {'pending': 'Waiting for exact-head image validation',
                        'success': 'Both required image validation jobs passed',
                        'failure': 'Image validation failed; see Validate run',
                        'error': 'Image validation did not complete'}
        require(state in descriptions, 'invalid-validation-status')
        target = 'https://github.com/' + self.api.repository + '/actions'
        if run_id is not None:
            require(type(run_id) is int and run_id > 0, 'invalid-validation-run')
            target += '/runs/' + str(run_id)
        for name in sorted(REQUIRED_JOBS):
            self.api('/statuses/' + head, 'POST', {'state': state, 'context': name,
                'description': descriptions[state], 'target_url': target})

    def report_validation_failure(self, run, jobs):
        # Report only fixed required names/states, never raw logs or job output.
        states = {'success', 'failure', 'cancelled', 'skipped', 'timed_out', 'action_required',
                  'neutral', 'startup_failure', 'stale'}
        results = {}
        for name in sorted(REQUIRED_JOBS):
            matching = [j for j in jobs if j.get('name') == name]
            result = matching[-1].get('conclusion') if matching else 'missing'
            results[name] = result if result in states else 'missing'
        target = 'https://github.com/' + self.api.repository + '/actions/runs/' + str(run['id'])
        print(json.dumps({'status': 'image-validation-failed', 'runUrl': target,
                          'requiredJobs': results}), flush=True)
        summary = os.environ.get('GITHUB_STEP_SUMMARY')
        if summary:
            with open(summary, 'a', encoding='utf-8') as output:
                output.write('## Terminal image validation failed\n\n'
                             '[Open the failing Validate run](' + target + '). The candidate remains unmerged.\n\n')
                for name, result in results.items(): output.write('- `' + name + '`: ' + result + '\n')

    def validate(self, branch, head):
        path = '/actions/workflows/ci.yml/runs?event=workflow_dispatch&branch=' + branch + '&per_page=100'
        def runs():
            return [r for r in self.api(path)['workflow_runs'] if r['head_sha'] == head and
                    r['event'] == 'workflow_dispatch' and r['head_branch'] == branch]
        self.validation_status(head, 'pending')
        try:
            existing = runs()
            if not existing:
                self.api('/actions/workflows/ci.yml/dispatches', 'POST', {'ref': branch})
            deadline = self.now() + 1800
            while self.now() < deadline:
                matching = runs()
                if matching:
                    run = max(matching, key=lambda r: r['id'])
                    if run['status'] == 'completed':
                        jobs = self.api.pages('/actions/runs/' + str(run['id']) + '/jobs', key='jobs')
                        if not passed_jobs(run, jobs, head):
                            self.validation_status(head, 'failure', run['id'])
                            self.report_validation_failure(run, jobs)
                            raise UpdateError('image-validation-failed-pr-left-open')
                        # Publish only after the entire workflow and both required jobs
                        # succeeded for this exact head, event and branch.
                        self.validation_status(head, 'success', run['id'])
                        return
                    require(run['status'] in ACTIVE, 'image-validation-not-running')
                self.sleep(15)
            raise UpdateError('image-validation-timeout-pr-left-open')
        except Exception as error:
            if str(error) != 'image-validation-failed-pr-left-open':
                self.validation_status(head, 'error')
            raise

    def merge(self, base, branch, head, number, lock):
        require(self.main_sha() == base, 'main-advanced-pr-left-open')
        pr = self.api('/pulls/' + str(number))
        require(managed_pr(pr, self.api.repository) and pr['state'] == 'open' and not pr['draft'] and
                pr['head']['sha'] == head and pr['head']['ref'] == branch and pr['base']['sha'] == base,
                'pull-request-changed-before-merge')
        files = self.api.pages('/pulls/' + str(number) + '/files')
        require(len(files) == 1 and files[0]['filename'] == 'images.lock.json' and
                files[0]['status'] == 'modified' and self.api.lock(head) == lock,
                'pull-request-not-terminal-image-only')
        result = self.api('/pulls/' + str(number) + '/merge', 'PUT', {'sha': head, 'merge_method': 'rebase'})
        require(result.get('merged'), 'protected-merge-rejected')
        return result['sha']

    def run(self, digest):
        base = self.main_sha()
        current = self.api.lock(base)
        updated = replace_terminal(current, digest)
        if current == updated:
            # Recover a failed dispatch after a previous successful bot merge.
            commit = self.api('/git/commits/' + base)
            automated = commit['author']['email'] == BOT['email'] and commit['message'].startswith('Update terminal image to ')
            dispatched = self.production(base) if automated else False
            return {'status': 'image-current', 'deploymentDispatched': dispatched}
        branch, head, number = self.candidate(base, digest, updated)
        print(json.dumps({'status': 'validating-image-update', 'pullRequest': number}), flush=True)
        self.validate(branch, head)
        merged = self.merge(base, branch, head, number, updated)
        dispatched = self.production(merged)
        return {'status': 'image-update-merged', 'pullRequest': number, 'deploymentDispatched': dispatched}


def main():
    require(os.environ.get('GITHUB_ACTIONS') == 'true' and os.environ.get('GITHUB_REF') == 'refs/heads/main' and
            os.environ.get('GITHUB_EVENT_NAME') in ['schedule', 'workflow_dispatch', 'push'], 'trusted-main-run-required')
    api = GitHub(os.environ['GITHUB_REPOSITORY'], os.environ['GH_TOKEN'])
    print(json.dumps(Updater(api).run(latest_digest()), sort_keys=True))


if __name__ == '__main__':
    try: main()
    except UpdateError as error:
        print(json.dumps({'status': 'image-update-failed', 'code': str(error)}), file=sys.stderr)
        sys.exit(1)
    except Exception:
        print(json.dumps({'status': 'image-update-failed', 'code': 'details-withheld'}), file=sys.stderr)
        sys.exit(1)
