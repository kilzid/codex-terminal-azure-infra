"""One-time privileged setup and short-lived local policy-reader certificates."""
import base64
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from cloud import Cloud, Failure, atomic, az, command, gh, require, claims

GRAPH_APP = '00000003-0000-0000-c000-000000000000'
GRAPH_ROLES = {'Application.ReadWrite.OwnedBy': '18a4783c-866b-4cc7-a460-3d5e5662c884',
               'Policy.Read.All': '246dd0d5-5bd0-4def-940b-0421030a5b68'}


def b64url(value): return base64.urlsafe_b64encode(value).rstrip(b'=').decode()


def oidc_subject(repository_id, owner_id):
    return f'repository_id:{repository_id}:repository_owner_id:{owner_id}:environment:production:ref:refs/heads/main'


@contextlib.contextmanager
def local_policy_reader(cloud):
    """A local admin is still checked by preflight. This certificate permits a
    live policy GET with the deployment app, without a stored client password.
    The public certificate expires in 15 minutes and is removed in finally.
    """
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        yield
        return
    cloud.preflight(administrator=True)
    p = cloud.private
    require(p.get('deploymentApplicationObjectId'), 'bootstrap-required-for-policy-reader')
    app_path = 'applications/' + p['deploymentApplicationObjectId']
    with tempfile.TemporaryDirectory(prefix='codex-policy-') as directory:
        directory = Path(directory)
        key, cert, der = [directory / n for n in ['key.pem', 'cert.pem', 'cert.der']]
        command(['openssl', 'req', '-x509', '-newkey', 'rsa:3072', '-nodes', '-keyout', str(key), '-out', str(cert),
                 '-subj', '/CN=codex-local-policy-reader', '-days', '1'], json_output=False)
        key.chmod(0o600)
        command(['openssl', 'x509', '-in', str(cert), '-outform', 'DER', '-out', str(der)], json_output=False)
        key_id = str(uuid.uuid4())
        now = dt.datetime.now(dt.timezone.utc)
        credential = {'keyId': key_id, 'type': 'AsymmetricX509Cert', 'usage': 'Verify',
                      'displayName': 'temporary-local-policy-reader',
                      'key': base64.b64encode(der.read_bytes()).decode(),
                      'startDateTime': now.isoformat(),
                      'endDateTime': (now + dt.timedelta(minutes=15)).isoformat()}
        existing = cloud.graph(app_path + '?$select=keyCredentials').get('keyCredentials', [])
        cloud.graph(app_path, 'PATCH', {'keyCredentials': existing + [credential]})
        old_reader = getattr(cloud, 'policy_token', None)
        try:
            cached_token = None
            def token():
                nonlocal cached_token
                if cached_token and claims(cached_token).get('exp', 0) > time.time() + 60: return cached_token
                endpoint = 'https://login.microsoftonline.com/' + p['tenantId'] + '/oauth2/v2.0/token'
                header = {'alg': 'RS256', 'typ': 'JWT', 'x5t': b64url(hashlib.sha1(der.read_bytes()).digest())}
                timestamp = int(time.time())
                body = {'aud': endpoint, 'iss': p['deploymentClientId'], 'sub': p['deploymentClientId'],
                        'jti': str(uuid.uuid4()), 'nbf': timestamp - 30, 'exp': timestamp + 300}
                unsigned = (b64url(json.dumps(header).encode()) + '.' + b64url(json.dumps(body).encode())).encode()
                signed = subprocess.run(['openssl', 'dgst', '-sha256', '-sign', str(key)], input=unsigned, capture_output=True)
                require(signed.returncode == 0, 'policy-assertion-signing-failed')
                assertion = unsigned.decode() + '.' + b64url(signed.stdout)
                data = urllib.parse.urlencode({'client_id': p['deploymentClientId'], 'scope': 'https://graph.microsoft.com/.default',
                    'grant_type': 'client_credentials', 'client_assertion_type': 'urn:ietf:params:oauth:client-assertion-type:jwt-bearer',
                    'client_assertion': assertion}).encode()
                req = urllib.request.Request(endpoint, data=data, headers={'Content-Type': 'application/x-www-form-urlencoded'})
                for attempt in range(36):
                    try:
                        with urllib.request.urlopen(req, timeout=60) as response: value = json.load(response)['access_token']
                        break
                    except urllib.error.HTTPError as error:
                        detail = json.loads(error.read())
                        if error.code != 401 or 'AADSTS700027' not in detail.get('error_description', ''): raise
                        require(attempt < 35, 'policy-credential-propagation-timeout')
                        time.sleep(5)
                c = claims(value)
                require(c['tid'] == p['tenantId'] and c['oid'] == p['deploymentObjectId'] and
                        c.get('appid', c.get('azp')) == p['deploymentClientId'], 'policy-reader-identity-mismatch')
                require('Policy.Read.All' in c.get('roles', []), 'policy-permission-not-propagated')
                cached_token = value
                return value
            cloud.policy_token = token
            yield
        finally:
            cloud.policy_token = old_reader
            current = cloud.graph(app_path + '?$select=keyCredentials').get('keyCredentials', [])
            cloud.graph(app_path, 'PATCH', {'keyCredentials': [v for v in current if v['keyId'] != key_id]})


def grant_role(cloud, principal, scope, suffix, actions):
    p = cloud.private
    subscription = '/subscriptions/' + p['subscriptionId']
    role_id = str(uuid.uuid5(uuid.NAMESPACE_URL, subscription + '/' + p['deploymentClientId'] + '/' + suffix))
    definition = subscription + '/providers/Microsoft.Authorization/roleDefinitions/' + role_id
    cloud.arm(definition, 'PUT', {'properties': {'roleName': 'Codex infrastructure ' + suffix + ' ' + role_id[:8],
        'description': 'Repository deployment: no permanent data deletion or role management.', 'type': 'CustomRole',
        'assignableScopes': [subscription], 'permissions': [{'actions': actions, 'notActions': [], 'dataActions': [], 'notDataActions': []}]}}, api='2022-04-01')
    assignment = scope + '/providers/Microsoft.Authorization/roleAssignments/' + str(uuid.uuid5(uuid.NAMESPACE_URL, scope + role_id + principal))
    cloud.arm(assignment, 'PUT', {'properties': {'roleDefinitionId': definition, 'principalId': principal,
                                               'principalType': 'ServicePrincipal'}}, api='2022-04-01')


def repository_binding(repo, expected):
    # Forks establish their own immutable identity and production environment;
    # they never inherit the source repository's secrets or Azure access.
    require(repo['full_name'].lower() == expected.lower() and not repo.get('archived'), 'bootstrap-repository-mismatch')
    require(repo.get('permissions', {}).get('admin') is True, 'repository-administration-required')
    return {'repositoryId': repo['id'], 'repositoryOwnerId': repo['owner']['id']}


def bootstrap(directory, *, publish=True):
    from controller import load_json, validate
    directory = Path(directory).expanduser().resolve()
    p = load_json(directory / 'private.json', 'bootstrap')
    if p['profile'] == 'adopted':
        require(all(p.get(k) for k in ['runtimeApplicationObjectId', 'runtimeServicePrincipalId', 'runtimeClientId', 'hostname', 'azureHostname']), 'existing-runtime-mappings-required-for-adoption')
    cloud = Cloud(p)
    cloud.preflight(administrator=True)
    repo = gh('repos/' + p['repository'])
    p.update(repository_binding(repo, p['repository']))
    gh('repos/' + p['repository'], 'PATCH', {'allow_rebase_merge': True, 'allow_merge_commit': False, 'allow_squash_merge': False})
    gh('repos/' + p['repository'] + '/branches/main/protection', 'PUT', {
        'required_status_checks': {'strict': True, 'contexts': ['validate', 'container-policies']},
        'enforce_admins': True, 'required_linear_history': True,
        'required_pull_request_reviews': {'dismiss_stale_reviews': True, 'require_code_owner_reviews': False, 'required_approving_review_count': 0},
        'restrictions': None, 'allow_force_pushes': False, 'allow_deletions': False})
    # The environment is restricted before any production secret is uploaded.
    gh('repos/' + p['repository'] + '/environments/production', 'PUT',
       {'wait_timer': 0, 'reviewers': [], 'deployment_branch_policy': {'protected_branches': False, 'custom_branch_policies': True}})
    policies = gh('repos/' + p['repository'] + '/environments/production/deployment-branch-policies')['branch_policies']
    for policy in policies:
        require(policy['name'] == 'main' and policy.get('type', 'branch') == 'branch', 'unexpected-environment-branch-policy')
    if not policies:
        gh('repos/' + p['repository'] + '/environments/production/deployment-branch-policies', 'POST', {'name': 'main', 'type': 'branch'})
    gh('repos/' + p['repository'] + '/actions/oidc/customization/sub', 'PUT',
       {'use_default': False, 'include_claim_keys': ['repository_id', 'repository_owner_id', 'context', 'ref']})
    gh('repos/' + p['repository'] + '/actions/permissions/workflow', 'PUT',
       {'default_workflow_permissions': 'read', 'can_approve_pull_request_reviews': False})
    if not p.get('deploymentApplicationObjectId'):
        app = cloud.graph('applications', 'POST', {'displayName': p['prefix'] + '-github-deployment', 'signInAudience': 'AzureADMyOrg',
            'requiredResourceAccess': [{'resourceAppId': GRAPH_APP, 'resourceAccess': [{'id': v, 'type': 'Role'} for v in GRAPH_ROLES.values()]}]})
        p.update(deploymentApplicationObjectId=app['id'], deploymentClientId=app['appId'])
        atomic(directory / 'private.json', p)
    if not p.get('deploymentObjectId'):
        principal = cloud.graph('servicePrincipals', 'POST', {'appId': p['deploymentClientId']})
        p['deploymentObjectId'] = principal['id']
        atomic(directory / 'private.json', p)
    cloud.private = p
    app_path = 'applications/' + p['deploymentApplicationObjectId']
    subject = oidc_subject(p['repositoryId'], p['repositoryOwnerId'])
    credentials = cloud.collection(app_path + '/federatedIdentityCredentials')
    existing = [v for v in credentials if v['name'] == 'production-main']
    require(all(v['name'] == 'production-main' for v in credentials), 'unexpected-federated-credential')
    credential = {'name': 'production-main', 'issuer': 'https://token.actions.githubusercontent.com',
                  'subject': subject, 'audiences': ['api://AzureADTokenExchange']}
    if existing:
        require(all(existing[0].get(k) == v for k, v in credential.items()), 'federated-credential-drift')
    else: cloud.graph(app_path + '/federatedIdentityCredentials', 'POST', credential)
    graph_sp = cloud.collection("servicePrincipals?$filter=appId eq '" + GRAPH_APP + "'")[0]
    assigned = cloud.collection('servicePrincipals/' + p['deploymentObjectId'] + '/appRoleAssignments')
    for role in GRAPH_ROLES.values():
        if not any(v['resourceId'] == graph_sp['id'] and v['appRoleId'] == role for v in assigned):
            cloud.graph('servicePrincipals/' + p['deploymentObjectId'] + '/appRoleAssignments', 'POST',
                        {'principalId': p['deploymentObjectId'], 'resourceId': graph_sp['id'], 'appRoleId': role})
    if not p.get('runtimeApplicationObjectId'):
        app = cloud.graph('applications', 'POST', {'displayName': p['prefix'] + '-runtime', 'signInAudience': 'AzureADMyOrg',
            'web': {'implicitGrantSettings': {'enableAccessTokenIssuance': False, 'enableIdTokenIssuance': False}}, 'requiredResourceAccess': []})
        p.update(runtimeApplicationObjectId=app['id'], runtimeClientId=app['appId'])
        atomic(directory / 'private.json', p)
    if not p.get('runtimeServicePrincipalId'):
        principal = cloud.graph('servicePrincipals', 'POST', {'appId': p['runtimeClientId'], 'appRoleAssignmentRequired': True})
        p['runtimeServicePrincipalId'] = principal['id']
        atomic(directory / 'private.json', p)
    for kind, identifier in [('applications', p['runtimeApplicationObjectId']), ('servicePrincipals', p['runtimeServicePrincipalId'])]:
        path = kind + '/' + identifier + '/owners'
        owners = cloud.collection(path)
        if not any(v['id'] == p['deploymentObjectId'] for v in owners):
            cloud.graph(path + '/$ref', 'POST', {'@odata.id': 'https://graph.microsoft.com/v1.0/directoryObjects/' + p['deploymentObjectId']})
    grants = cloud.collection('servicePrincipals/' + p['runtimeServicePrincipalId'] + '/appRoleAssignedTo')
    require(all(v['principalId'] == p['userObjectId'] for v in grants), 'unexpected-runtime-assignment')
    if not grants:
        cloud.graph('servicePrincipals/' + p['runtimeServicePrincipalId'] + '/appRoleAssignedTo', 'POST',
                    {'principalId': p['userObjectId'], 'resourceId': p['runtimeServicePrincipalId'], 'appRoleId': str(uuid.UUID(int=0))})
    group = az('group', 'exists', '--name', p['resourceGroup'], '--subscription', p['subscriptionId'])
    if not group:
        require(p['profile'] == 'deployment', 'adoption-resource-group-missing')
        az('group', 'create', '--name', p['resourceGroup'], '--location', p['location'],
           '--tags', 'application=codex-terminal', '--subscription', p['subscriptionId'])
    require(az('group', 'show', '--name', p['resourceGroup'], '--subscription', p['subscriptionId']).get('tags', {}).get('application') == 'codex-terminal', 'resource-group-ownership-required')
    for provider in ['Microsoft.Compute', 'Microsoft.Network', 'Microsoft.RecoveryServices', 'Microsoft.Insights', 'Microsoft.Consumption', 'Microsoft.Storage', 'Microsoft.OperationalInsights']:
        if az('provider', 'show', '--namespace', provider, '--subscription', p['subscriptionId'])['registrationState'] != 'Registered':
            az('provider', 'register', '--namespace', provider, '--wait', '--subscription', p['subscriptionId'])
    grant_role(cloud, p['deploymentObjectId'], cloud.rg, 'runtime', [
        '*/read', 'Microsoft.Resources/deployments/*',
        'Microsoft.Compute/virtualMachines/write', 'Microsoft.Compute/virtualMachines/start/action',
        'Microsoft.Compute/virtualMachines/restart/action', 'Microsoft.Compute/virtualMachines/deallocate/action',
        'Microsoft.Compute/virtualMachines/runCommands/write', 'Microsoft.Compute/virtualMachines/runCommands/delete',
        'Microsoft.Compute/disks/write', 'Microsoft.Network/virtualNetworks/write', 'Microsoft.Network/virtualNetworks/subnets/write',
        'Microsoft.Network/virtualNetworks/subnets/join/action', 'Microsoft.Network/networkSecurityGroups/write',
        'Microsoft.Network/networkSecurityGroups/join/action', 'Microsoft.Network/networkInterfaces/write',
        'Microsoft.Network/networkInterfaces/join/action', 'Microsoft.Network/publicIPAddresses/write', 'Microsoft.Network/publicIPAddresses/join/action',
        'Microsoft.RecoveryServices/vaults/write', 'Microsoft.RecoveryServices/vaults/backupPolicies/write',
        'Microsoft.RecoveryServices/vaults/backupFabrics/protectionContainers/write',
        'Microsoft.RecoveryServices/vaults/backupFabrics/protectionContainers/protectedItems/write',
        'Microsoft.RecoveryServices/vaults/backupFabrics/protectionContainers/protectedItems/backup/action',
        'Microsoft.Insights/actionGroups/write', 'Microsoft.Insights/metricAlerts/write',
        'Microsoft.Storage/storageAccounts/write', 'Microsoft.Storage/storageAccounts/fileServices/write',
        'Microsoft.Storage/storageAccounts/fileServices/shares/write', 'Microsoft.OperationalInsights/workspaces/write'])
    # Budget creation and scope ownership are administrator work, not subscription-wide CI rights.
    from controller import Controller
    validate(p, 'private')
    atomic(directory / 'private.json', p)
    ctl = Controller(directory)
    try: cloud.arm(ctl.budget_path, api='2024-08-01')
    except urllib.error.HTTPError as error:
        if error.code != 404: raise
        cloud.arm(ctl.budget_path, 'PUT', {'properties': ctl.desired_budget()}, api='2024-08-01')
    grant_role(cloud, p['deploymentObjectId'], ctl.budget_path, 'budget', ['Microsoft.Consumption/budgets/read', 'Microsoft.Consumption/budgets/write'])
    # Audit the effective config; bootstrap itself never restarts VM services.
    with local_policy_reader(cloud): cloud.mfa()
    if publish: publish_secrets(directory, p)
    return {'status': 'bootstrap-complete'}


def publish_secrets(directory, private):
    values = {'PRIVATE_CONFIG': json.dumps(private), 'AZURE_CLIENT_ID': private['deploymentClientId'],
              'AZURE_TENANT_ID': private['tenantId'], 'AZURE_SUBSCRIPTION_ID': private['subscriptionId']}
    path = Path(directory) / 'runtime-secrets.json'
    if path.exists(): values['RUNTIME_SECRETS'] = path.read_text()
    for name, value in values.items():
        command(['gh', 'secret', 'set', name, '--repo', private['repository'], '--env', 'production'], data=value, json_output=False)
