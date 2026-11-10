"""Quiet cloud clients. Command output and API bodies never reach CI logs."""
import base64
import datetime as dt
import json
import gzip
import hashlib
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

ARM = 'https://management.azure.com'
GRAPH = 'https://graph.microsoft.com/v1.0/'


class Failure(RuntimeError):
    """Only fixed, public error codes may be used as messages."""


def require(value, code):
    if not value: raise Failure(code)


def command(args, *, data=None, json_output=True):
    result = subprocess.run(args, input=data, capture_output=True, text=True)
    if result.returncode:
        # Identify the failed operation without exposing arguments, output or account data.
        stages = {('az', 'backup', 'protection', 'backup-now'): 'azure-backup-trigger',
                  ('az', 'backup', 'job', 'show'): 'azure-backup-status',
                  ('az', 'deployment', 'group', 'what-if'): 'azure-infrastructure-preview',
                  ('az', 'deployment', 'group', 'create'): 'azure-infrastructure-deployment',
                  ('az', 'vm', 'deallocate'): 'azure-vm-deallocate',
                  ('az', 'vm', 'resize'): 'azure-vm-resize',
                  ('az', 'vm', 'start'): 'azure-vm-start'}
        stage = next((name for prefix, name in stages.items() if tuple(args[:len(prefix)]) == prefix),
                     'azure-command' if args and args[0] == 'az' else 'external-command')
        # Only these public service codes may be reflected in the fixed error code.
        denied = re.search(r'\((?:AuthorizationFailed|LinkedAuthorizationFailed|Forbidden)\)', result.stderr or '')
        raise Failure(stage + ('-authorization-failed' if denied else '-failed'))
    return json.loads(result.stdout) if json_output and result.stdout.strip() else result.stdout


def az(*args):
    return command(['az', *args, '--only-show-errors', '-o', 'json'])


def gh(path, method='GET', body=None):
    return command(['gh', 'api', '--method', method, path, *(['--input', '-'] if body is not None else [])],
                   data=json.dumps(body) if body is not None else None)


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def claims(token):
    part = token.split('.')[1]
    return json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))


def request(url, *, method='GET', body=None, token=None):
    headers = {'Content-Type': 'application/json'}
    if token: headers['Authorization'] = 'Bearer ' + token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, headers=headers, data=data)
    # Deliberately no automatic retry of credential-creation POSTs.
    with urllib.request.urlopen(req, timeout=90) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}


class Cloud:
    def __init__(self, private, recovery_directory=None):
        self.private = private
        self.recovery_directory = Path(recovery_directory) if recovery_directory else None
        self.pending_command_archive = None
        self.tokens = {}

    @property
    def rg(self):
        p = self.private
        return '/subscriptions/' + p['subscriptionId'] + '/resourceGroups/' + p['resourceGroup']

    @property
    def vm(self):
        return self.rg + '/providers/Microsoft.Compute/virtualMachines/' + self.private['resources']['vmName']

    def token(self, audience):
        if audience in self.tokens and claims(self.tokens[audience]).get('exp', 0) <= time.time() + 120:
            self.tokens.pop(audience)
        if audience not in self.tokens:
            value = az('account', 'get-access-token', '--subscription', self.private['subscriptionId'],
                       '--resource', audience)['accessToken']
            c = claims(value)
            require(c.get('tid') == self.private['tenantId'], 'token-tenant-mismatch')
            expected = self.private['deploymentObjectId'] if os.environ.get('GITHUB_ACTIONS') == 'true' else self.private['adminObjectId']
            require(c.get('oid') == expected, 'token-identity-mismatch')
            if os.environ.get('GITHUB_ACTIONS') == 'true':
                require(c.get('appid', c.get('azp')) == self.private['deploymentClientId'], 'token-client-mismatch')
            self.tokens[audience] = value
        return self.tokens[audience]

    def preflight(self, *, administrator=False):
        p = self.private
        account = az('account', 'show', '--subscription', p['subscriptionId'])
        require(account['id'] == p['subscriptionId'] and account['tenantId'] == p['tenantId'], 'account-identity-mismatch')
        require(account.get('state') == 'Enabled' and account.get('environmentName') == 'AzureCloud', 'account-unavailable')
        ci = os.environ.get('GITHUB_ACTIONS') == 'true'
        require(not (administrator and ci), 'administrator-operation-forbidden-in-ci')
        if ci:
            require(os.environ.get('GITHUB_REF') == 'refs/heads/main', 'ci-branch-rejected')
            require(os.environ.get('GITHUB_EVENT_NAME') in ['push', 'schedule', 'workflow_dispatch'], 'ci-event-rejected')
            require(os.environ.get('GITHUB_REPOSITORY_ID') == str(p['repositoryId']), 'ci-repository-rejected')
            require(os.environ.get('GITHUB_REPOSITORY_OWNER_ID') == str(p['repositoryOwnerId']), 'ci-owner-rejected')
            require(account.get('user', {}).get('type') == 'servicePrincipal', 'ci-login-type-rejected')
        else:
            require(account.get('user', {}).get('type') == 'user', 'administrator-login-type-rejected')
        self.token(ARM + '/')
        self.token('https://graph.microsoft.com/')

    def graph(self, path, method='GET', body=None):
        url = path if path.startswith(GRAPH) else GRAPH + path
        require(url.startswith(GRAPH), 'graph-url-rejected')
        return request(url.replace(' ', '%20'), method=method, body=body, token=self.token('https://graph.microsoft.com/'))

    def collection(self, path):
        rows = []
        while path:
            value = self.graph(path)
            rows.extend(value.get('value', []))
            path = value.get('@odata.nextLink')
        return rows

    def arm(self, path, method='GET', body=None, api='2024-11-01'):
        require(path.lower().startswith('/subscriptions/' + self.private['subscriptionId'].lower() + '/'), 'subscription-scope-rejected')
        return request(ARM + path + '?api-version=' + api, method=method, body=body, token=self.token(ARM + '/'))

    def mfa(self):
        value = request(GRAPH + 'policies/identitySecurityDefaultsEnforcementPolicy',
                        token=self.policy_token() if getattr(self, 'policy_token', None) else self.token('https://graph.microsoft.com/'))
        require(value.get('isEnabled') is True, 'live-mfa-policy-required')

    def identity(self, *, callback=None):
        p = self.private
        app = self.graph('applications/' + p['runtimeApplicationObjectId'])
        principal = self.graph('servicePrincipals/' + p['runtimeServicePrincipalId'])
        require(app['appId'] == p['runtimeClientId'] and principal['appId'] == p['runtimeClientId'], 'runtime-identity-mismatch')
        require(app['signInAudience'] == 'AzureADMyOrg' and principal['appRoleAssignmentRequired'] is True, 'runtime-access-policy-drift')
        grants = self.collection('servicePrincipals/' + p['runtimeServicePrincipalId'] + '/appRoleAssignedTo')
        require(len(grants) == 1 and grants[0]['principalId'] == p['userObjectId'], 'runtime-owner-assignment-drift')
        implicit = app['web'].get('implicitGrantSettings', {})
        require(not implicit.get('enableAccessTokenIssuance') and not implicit.get('enableIdTokenIssuance'), 'implicit-grant-rejected')
        if callback: require(app['web'].get('redirectUris') == [callback], 'callback-drift')
        return app

    def run_host(self, script, payload, *, timeout=900, name='reconcile'):
        # Reuse one owned execution resource: Azure permits only 25 per VM,
        # and can reject updates while already at that limit.
        if not getattr(self, 'command_path', None):
            values = self.arm(self.vm + '/runCommands').get('value', [])
            owned = {v['id'].rsplit('/', 1)[-1]: v for v in values
                     if v['id'].lower().startswith(self.vm.lower() + '/runcommands/') and
                     v['id'].rsplit('/', 1)[-1].startswith('codex-infra-')}
            preferred = next((owned[n]['id'] for n in ['codex-infra-controller', 'codex-infra-plan'] if n in owned), None)
            self.command_path = preferred or next((v['id'] for v in owned.values()), self.vm + '/runCommands/codex-infra-controller')
            obsolete = [v for v in owned.values() if v['id'] != self.command_path]
            if len(values) >= 24:
                require(obsolete and self.recovery_directory, 'run-command-capacity-needs-administrator-cleanup')
                archive = {'savedAt': dt.datetime.now(dt.timezone.utc).isoformat(), 'records': obsolete}
                archive_id = hashlib.sha256(json.dumps(archive, sort_keys=True).encode()).hexdigest()
                # Durable local copy precedes deletion. The wrapper also stores a
                # root-only VM copy on the next successful execution.
                atomic(self.recovery_directory / 'run-command-archives' / (archive_id + '.json'), archive)
                self.pending_command_archive = archive
                for previous in obsolete: self.arm(previous['id'], 'DELETE')
        path = self.command_path
        body_payload = {**payload, **({'_runCommandArchive': self.pending_command_archive} if self.pending_command_archive else {})}
        encoded = base64.b64encode(gzip.compress(json.dumps(body_payload).encode())).decode()
        invocation = secrets.token_hex(16)
        wrapper = """import base64 as _cb, gzip as _cg, json as _cj, os as _co, pathlib as _cp, hashlib as _ch
_dp = _cj.loads(_cg.decompress(_cb.b64decode(_co.environ['CODEX_VM_PAYLOAD'])))
_archive = _dp.pop('_runCommandArchive', None)
if _archive:
 _data = _cj.dumps(_archive, sort_keys=True).encode()
 _directory = _cp.Path('/etc/codex-terminal/run-command-archive')
 _directory.mkdir(mode=0o700, parents=True, exist_ok=True)
 _file = _directory / (_ch.sha256(_data).hexdigest() + '.json')
 if not _file.exists():
  _file.write_bytes(_data)
  _file.chmod(0o600)
_co.environ['CODEX_VM_PAYLOAD'] = _cb.b64encode(_cj.dumps(_dp).encode()).decode()
"""
        wrapper += 'exec(compile(' + repr(script) + ', "<codex-host>", "exec"), {"__name__":"__main__", "__file__":"<codex-host>"})\n'
        shell = "#!/bin/bash\nset -eu\numask 077\nprintf 'CODEX_INVOCATION=" + invocation + "\\n'\npython3 - <<'CODEX_HOST'\n" + wrapper + '\nCODEX_HOST\n'
        body = {'location': self.private['location'], 'properties': {
            'source': {'script': shell},
            'asyncExecution': False, 'timeoutInSeconds': timeout, 'treatFailureAsDeploymentFailure': False,
            'parameters': [], 'protectedParameters': [{'name': 'CODEX_VM_PAYLOAD', 'value': encoded}]}}
        self.arm(path, 'PUT', body)
        deadline = time.monotonic() + timeout + 180
        while time.monotonic() < deadline:
            value = request(ARM + path + '?api-version=2024-11-01&$expand=instanceView', token=self.token(ARM + '/'))
            view = value.get('properties', {}).get('instanceView', {})
            status = view.get('executionState')
            fresh = ('CODEX_INVOCATION=' + invocation) in view.get('output', '')
            if status in ['Succeeded', 'Failed', 'Canceled', 'TimedOut'] and fresh:
                if status != 'Succeeded' or view.get('exitCode') != 0:
                    match = re.search(r'CODEX_OPERATION_FAILED:([a-z-]+):([A-Za-z]+)', view.get('error', ''))
                    allowed = ['protocol', 'plan', 'os-readiness', 'pull-images', 'install-configuration', 'start-services', 'verify-runtime', 'verify-services', 'verify-identity', 'verify-metadata', 'verify-sandbox', 'verify-native', 'native-http', 'native-origin', 'native-create-session', 'native-websocket', 'native-attach', 'native-command', 'adopt', 'apply', 'verify', 'rollback', 'commit']
                    phase = match[1] if match and match[1] in allowed else 'operation'
                    raise Failure('host-' + phase + '-failed')
                match = re.search(r'^CODEX_RESULT=([A-Za-z0-9+/=]+)$', view.get('output', ''), re.M)
                require(match, 'host-protocol-invalid')
                self.pending_command_archive = None
                return json.loads(base64.b64decode(match[1], validate=True))
            time.sleep(5)
        raise Failure('host-operation-timeout')

    def backup(self, retention_days):
        p = self.private
        until = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=retention_days)).strftime('%d-%m-%Y')
        result = az('backup', 'protection', 'backup-now', '--resource-group', p['resourceGroup'],
                    '--vault-name', p['resources']['vaultName'], '--container-name', p['resources']['backupContainer'],
                    '--item-name', p['resources']['backupItem'], '--backup-management-type', 'AzureIaasVM',
                    '--retain-until', until, '--subscription', p['subscriptionId'])
        job_id = result.get('name') or result.get('properties', {}).get('jobId')
        require(job_id, 'backup-job-missing')
        deadline = time.monotonic() + 3600
        while time.monotonic() < deadline:
            job = az('backup', 'job', 'show', '--resource-group', p['resourceGroup'], '--vault-name', p['resources']['vaultName'],
                     '--name', job_id, '--subscription', p['subscriptionId'])
            status = job['properties']['status']
            if status == 'Completed': return
            require(status in ['InProgress', 'Pending'], 'backup-failed')
            time.sleep(20)
        raise Failure('backup-timeout-no-mutation-performed')
