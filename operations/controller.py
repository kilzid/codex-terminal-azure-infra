"""Repository-defined plans, private inputs and deployment orchestration."""
import base64
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from jsonschema import Draft202012Validator, FormatChecker
from preview import meaningful_changes
from cloud import Cloud, Failure, atomic, az, command, gh, require

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runtime'))
from render import compose, proxy_config, caddy_config


def validate(value, name):
    schema = json.loads((ROOT / 'schemas' / (name + '.schema.json')).read_text())
    require(not list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value)), 'invalid-' + name + '-configuration')
    return value


def load_json(path, schema=None):
    value = json.loads(Path(path).read_text())
    return validate(value, schema) if schema else value


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def git_commit():
    return command(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], json_output=False).strip()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl): return None


class Controller:
    def __init__(self, directory, *, profile=None):
        self.directory = Path(directory).expanduser().resolve()
        require(self.directory != ROOT and ROOT not in self.directory.parents, 'private-inputs-must-be-outside-checkout')
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self.private = load_json(self.directory / 'private.json', 'private')
        self.secrets = load_json(self.directory / 'runtime-secrets.json', 'runtime-secrets') if (self.directory / 'runtime-secrets.json').exists() else None
        self.profile = profile or self.private['profile']
        require(self.profile in ['deployment', 'adopted'], 'unknown-deployment-profile')
        self.settings = load_json(ROOT / 'config' / (self.profile + '.json'), 'deployment')
        self.images = load_json(ROOT / 'images.lock.json', 'images')
        self.cloud = Cloud(self.private, self.directory)
        self.commit = git_commit()
        for path in self.directory.glob('*.json'): path.chmod(0o600)

    def desired(self):
        require(self.secrets is not None, 'runtime-secrets-required')
        p = self.private
        require(p.get('hostname') and p.get('azureHostname'), 'hostnames-not-discovered')
        files = {
            'compose.json': json.dumps(compose(self.images, self.settings['vmSize']), indent=2) + '\n',
            'oauth2-proxy.cfg': proxy_config(p['tenantId'], p['runtimeClientId'], p['userObjectId'], p['hostname'],
                                             self.secrets['clientSecret'], self.secrets['cookieSecret']),
            'Caddyfile': caddy_config(p['hostname'], p['azureHostname'], p['contactEmail']),
            **{n: (ROOT / 'runtime' / n).read_text() for n in ['seccomp.json', 'apparmor.profile',
                 'network-rules.sh', 'boot-guards.sh', 'codex-config.py', 'docker-daemon.json',
                 'sysctl.conf', 'unattended-upgrades.conf', 'terminal.service']}}
        return {k: base64.b64encode(v.encode()).decode() for k, v in files.items()}

    def payload(self, action):
        p = self.private
        files = self.desired()
        return {'action': action, 'files': files, 'commit': self.commit, 'images': self.images,
                'fingerprint': fingerprint({'settings': self.settings, 'images': self.images, 'private': p, 'files': files}),
                'identity': {'tenant': p['tenantId'], 'client': p['runtimeClientId'], 'owner': p['userObjectId'], 'host': p['hostname']},
                'sandbox': base64.b64encode((ROOT / 'runtime/sandbox-check.sh').read_bytes()).decode(),
                'nativeProbe': base64.b64encode((ROOT / 'runtime/native-probe.mjs').read_bytes()).decode(),
                'runtimeRevisions': self.settings['retention']['runtimeRevisions'],
                'runCommandDays': self.settings['retention']['runCommandDays']}

    def host(self, action, **extra):
        return self.cloud.run_host((ROOT / 'runtime/host.py').read_text(), {**self.payload(action), **extra}, name=action)

    def parameters(self):
        p, s = self.private, self.settings
        optional = s['optional']
        require(not optional['fileShareBackups'] or optional['retainedRecovery'], 'file-share-backups-require-retained-storage')
        if optional['fileShareBackups']: require(p['resources'].get('fileBackupItemNames'), 'existing-file-backup-mappings-required')
        if optional['retainedRecovery']: require(p['resources'].get('recoveryStorageName'), 'recovery-storage-mapping-required')
        if optional['auditLogging']: require(p['resources'].get('auditWorkspaceName'), 'audit-workspace-mapping-required')
        metadata = {}
        if optional['retainedRecovery']:
            storage = self.cloud.rg + '/providers/Microsoft.Storage/storageAccounts/' + p['resources']['recoveryStorageName']
            for share, key in [('workspace', 'workspaceMetadata'), ('codex-config', 'configMetadata')]:
                value = self.cloud.arm(storage + '/fileServices/default/shares/' + share, api='2024-01-01')['properties'].get('metadata') or {}
                require(all(k.lower().startswith('azurebackup') for k in value), 'unmanaged-recovery-share-metadata')
                metadata[key] = value
        return {'location': p['location'], 'prefix': p['prefix'], 'vmSize': s['vmSize'],
                'adminPublicKey': p['adminPublicKey'], 'contactEmail': p['contactEmail'], 'ownerObjectId': p['userObjectId'],
                'imageVersion': self.images['os']['version'], **metadata,
                'storageEndpointLocations': p['resources'].get('storageEndpointLocations', [p['location']]),
                'fileBackupItemNames': p['resources'].get('fileBackupItemNames', []),
                'dailyRetentionDays': s['backup']['dailyRetentionDays'], 'instantRetentionDays': s['backup']['instantRetentionDays'],
                'memoryPercent': s['monitor']['memoryPercent'], 'cpuCredits': s['monitor']['cpuCredits'],
                'auditDays': s['retention']['auditDays'],
                **optional, 'recoveryStorageName': p['resources'].get('recoveryStorageName', ''),
                'auditWorkspaceName': p['resources'].get('auditWorkspaceName', '')}

    def deployment(self, operation):
        p = self.private
        # Only this short-lived file contains ARM deployment parameters. Never an artifact.
        fd, filename = tempfile.mkstemp(dir=self.directory, suffix='.parameters.json')
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump({'parameters': {k: {'value': v} for k, v in self.parameters().items()}}, stream)
            args = ['deployment', 'group', operation, '--name', 'codex-infra', '--resource-group', p['resourceGroup'],
                    '--template-file', str(ROOT / 'infra/main.bicep'), '--parameters', '@' + filename,
                    '--subscription', p['subscriptionId'], '--mode', 'Incremental']
            if operation == 'what-if': args += ['--result-format', 'FullResourcePayloads', '--no-pretty-print']
            result = az(*args)
            if operation == 'what-if': atomic(self.directory / 'what-if.json', result)
            return result
        finally:
            os.unlink(filename)

    def infrastructure_plan(self):
        result = self.deployment('what-if')
        require(result.get('status') == 'Succeeded', 'infrastructure-preview-failed')
        changes = meaningful_changes(result, self.parameters())
        # Incremental deployments plus restricted RBAC never delete retained resources.
        require(all(v.get('changeType') in ['Create', 'Modify', 'Deploy'] for v in changes), 'destructive-infrastructure-plan-rejected')
        for item in changes:
            if item.get('resourceId', '').lower() == self.cloud.vm.lower() and item.get('changeType') == 'Create':
                require(self.private['profile'] == 'deployment' and not self.private['resources'].get('vmInstanceId'), 'automatic-vm-recreation-rejected')
            if item.get('resourceId', '').lower() == self.cloud.vm.lower() and item.get('changeType') != 'Create':
                for delta in item.get('delta') or []:
                    path = delta.get('path', '').lower()
                    require(not any(x in path for x in ['storageprofile', 'osprofile', 'networkprofile', 'location']), 'vm-replacement-or-immutable-change-rejected')
        return changes

    @property
    def budget_path(self):
        return '/subscriptions/' + self.private['subscriptionId'] + '/providers/Microsoft.Consumption/budgets/' + self.private['budgetName']

    def desired_budget(self):
        p = self.private
        return {'amount': self.settings['monthlyBudget'], 'category': 'Cost', 'timeGrain': 'Monthly',
                'timePeriod': {'startDate': p['budgetStartDate']},
                'filter': {'dimensions': {'name': 'ResourceGroupName', 'operator': 'In',
                          'values': [v.lower() for v in p['resources'].get('budgetResourceGroups', [p['resourceGroup'], p['resources']['backupSnapshotResourceGroup']])]}},
                'notifications': {'actual-' + str(n): {'enabled': True, 'operator': 'GreaterThanOrEqualTo',
                    'threshold': n, 'thresholdType': 'Actual', 'contactEmails': [p['contactEmail']]} for n in [30, 60, 90]}}

    def budget_changed(self):
        desired = self.desired_budget()
        actual = self.cloud.arm(self.budget_path, api='2024-08-01')['properties']
        # Azure adds a default endDate and read-only currentSpend/forecastSpend.
        comparison = {k: actual.get(k) for k in desired}
        comparison['timePeriod'] = {'startDate': actual['timePeriod']['startDate']}
        for notification in comparison.get('notifications', {}).values():
            for key in ['contactGroups', 'contactRoles', 'locale']:
                if not notification.get(key): notification.pop(key, None)
        return comparison != desired

    def public_verify(self, *, hostname=None):
        p = self.private
        opener = urllib.request.build_opener(NoRedirect())
        base = 'https://' + (hostname or p['hostname'])
        probes = [('/', {}), ('/api/sessions', {}), ('/ws', {'Connection': 'Upgrade', 'Upgrade': 'websocket',
                   'Sec-WebSocket-Version': '13', 'Sec-WebSocket-Key': 'dGhlIHNhbXBsZSBub25jZQ=='}),
                  ('/', {'Cookie': '__Host-codex_terminal=forged'})]
        for path, headers in probes:
            try: response = opener.open(urllib.request.Request(base + path, headers=headers), timeout=30)
            except urllib.error.HTTPError as error: response = error
            try:
                target = urllib.parse.urlsplit(response.headers.get('Location', ''))
                query = urllib.parse.parse_qs(target.query)
                require(response.code in [302, 303, 307] and target.hostname == 'login.microsoftonline.com'
                        and target.path.startswith('/' + p['tenantId'] + '/')
                        and query.get('client_id') == [p['runtimeClientId']]
                        and query.get('redirect_uri') == [base + '/oauth2/callback'], 'public-entra-boundary-failed')
            finally: response.close()

    def bind_resource_identity(self, vm, ip):
        disk_id = vm['properties']['storageProfile']['osDisk']['managedDisk']['id']
        disk = self.cloud.arm(disk_id, api='2024-03-02')
        self.private['resources'].update(vmInstanceId=vm['properties']['vmId'], osDiskId=disk_id,
            osDiskUniqueId=disk['properties']['uniqueId'], publicIpId=ip['id'])
        validate(self.private, 'private')
        atomic(self.directory / 'private.json', self.private)

    def guard_resource_identity(self):
        expected = self.private['resources']
        if not expected.get('vmInstanceId'): return
        vm = self.cloud.arm(self.cloud.vm)
        require(vm['properties']['vmId'] == expected['vmInstanceId'], 'vm-instance-replacement-rejected')
        disk_id = vm['properties']['storageProfile']['osDisk']['managedDisk']['id']
        require(disk_id.lower() == expected['osDiskId'].lower(), 'os-disk-replacement-rejected')
        disk = self.cloud.arm(disk_id, api='2024-03-02')
        require(disk['properties']['uniqueId'] == expected['osDiskUniqueId'], 'os-disk-instance-replacement-rejected')
        nic = self.cloud.arm(vm['properties']['networkProfile']['networkInterfaces'][0]['id'], api='2024-05-01')
        ip_id = nic['properties']['ipConfigurations'][0]['properties']['publicIPAddress']['id']
        require(ip_id.lower() == expected['publicIpId'].lower(), 'public-ip-resource-replacement-rejected')
        ip = self.cloud.arm(ip_id, api='2024-05-01')
        require(ip['properties']['ipAddress'] == expected['publicIp'], 'public-ip-address-change-rejected')

    def actual_infrastructure(self):
        p = self.private
        vm = self.cloud.arm(self.cloud.vm)
        require(vm['properties']['hardwareProfile']['vmSize'] == self.settings['vmSize'], 'vm-size-drift')
        require(not vm.get('identity', {}).get('principalId') and not vm.get('identity', {}).get('userAssignedIdentities'), 'vm-managed-identity-rejected')
        require(vm['properties']['storageProfile']['osDisk']['deleteOption'] == 'Detach', 'disk-delete-policy-drift')
        require(vm['properties']['storageProfile']['imageReference']['version'] == self.images['os']['version'], 'os-image-drift-requires-replacement-plan')
        interfaces = vm['properties']['networkProfile']['networkInterfaces']
        require(len(interfaces) == 1, 'network-interface-drift')
        nic = self.cloud.arm(interfaces[0]['id'], api='2024-05-01')
        addresses = nic['properties']['ipConfigurations']
        require(len(addresses) == 1, 'network-address-drift')
        subnet = self.cloud.arm(addresses[0]['properties']['subnet']['id'], api='2024-05-01')
        ip = self.cloud.arm(addresses[0]['properties']['publicIPAddress']['id'], api='2024-05-01')
        require(ip['properties']['publicIPAllocationMethod'] == 'Static', 'ip-allocation-drift')
        if p['resources'].get('publicIp'): require(ip['properties']['ipAddress'] == p['resources']['publicIp'], 'public-ip-changed')
        nsg = self.cloud.arm(subnet['properties']['networkSecurityGroup']['id'], api='2024-05-01')
        rules = [v['properties'] for v in nsg['properties']['securityRules'] if v['properties']['direction'] == 'Inbound']
        deny = [v for v in rules if v['access'] == 'Deny' and v['protocol'] == '*' and
                all(v.get(k) == '*' for k in ['sourceAddressPrefix', 'destinationAddressPrefix', 'sourcePortRange', 'destinationPortRange'])]
        require(deny, 'inbound-deny-rule-missing')
        require(all(v['protocol'].lower() == 'tcp' and v.get('destinationPortRange') in ['80', '443'] and
                    v['priority'] < min(d['priority'] for d in deny) for v in rules if v['access'] == 'Allow'), 'inbound-access-drift')
        baseline = self.directory / 'adoption-baseline.json'
        if baseline.exists():
            old = load_json(baseline)
            require(vm['id'] == old['vmId'] and vm['properties']['vmId'] == old['instanceId'] and
                    vm['properties']['storageProfile']['osDisk']['managedDisk']['id'] == old['diskId'] and
                    ip['id'] == old['ipId'] and ip['properties']['ipAddress'] == old['ipAddress'], 'adopted-resource-replacement-rejected')
        return vm, ip

    def plan(self):
        self.cloud.preflight()
        self.cloud.mfa()
        app = self.cloud.identity()
        self.guard_resource_identity()
        infrastructure = self.infrastructure_plan()
        host = self.host('plan')
        result = {'infrastructure': infrastructure, 'host': host,
                  'budgetChanged': self.budget_changed(),
                  'callbackChanged': app['web']['redirectUris'] != ['https://' + self.private['hostname'] + '/oauth2/callback']}
        atomic(self.directory / 'plan.json', result)
        return result

    def apply_infrastructure(self):
        machine = self.cloud.arm(self.cloud.vm)
        previous = machine['properties']['hardwareProfile']['vmSize']
        resizing = previous != self.settings['vmSize']
        args = ['--ids', self.cloud.vm, '--subscription', self.private['subscriptionId']]
        if resizing: az('vm', 'deallocate', *args)
        try:
            self.deployment('create')
            if resizing: az('vm', 'start', *args)
        except Exception:
            if resizing:
                az('vm', 'resize', *args, '--size', previous)
                az('vm', 'start', *args)
            raise

    def apply(self):
        plan = self.plan()
        # First adoption is a separately verified no-op, never an implicit reconcile.
        require(plan['host'].get('applied') is not None or self.private['profile'] == 'deployment', 'adoption-required-before-apply')
        disruptive = plan['infrastructure'] or plan['host']['changed'] or plan['host'].get('serviceDrift') or plan['callbackChanged']
        if disruptive: self.cloud.backup(self.settings['backup']['dailyRetentionDays'])
        if plan['infrastructure']: self.apply_infrastructure()
        if plan['budgetChanged']: self.cloud.arm(self.budget_path, 'PUT', {'properties': self.desired_budget()}, api='2024-08-01')
        app = self.cloud.identity()
        web = app['web']
        callback = 'https://' + self.private['hostname'] + '/oauth2/callback'
        result = None
        try:
            if plan['callbackChanged']:
                self.cloud.graph('applications/' + self.private['runtimeApplicationObjectId'], 'PATCH',
                                 {'web': {**web, 'redirectUris': sorted(set(web['redirectUris'] + [callback]))}})
            result = self.host('apply', expectedBefore=plan['host']['before'])
            self.public_verify()
            if plan['callbackChanged']:
                self.cloud.graph('applications/' + self.private['runtimeApplicationObjectId'], 'PATCH',
                                 {'web': {**web, 'redirectUris': [callback]}})
            self.actual_infrastructure()
            self.cloud.identity(callback=callback)
            self.host('commit')
            self.expire_run_commands()
        except Exception:
            if result and result.get('revision'): self.host('rollback', revision=result['revision'], automaticRollback=True)
            if plan['callbackChanged']:
                self.cloud.graph('applications/' + self.private['runtimeApplicationObjectId'], 'PATCH', {'web': web})
            raise
        return {'status': result['status'], 'changedFiles': len(result.get('changed', [])),
                'affectedServices': result.get('affected', []), 'infrastructureChanges': len(plan['infrastructure'])}

    def verify(self):
        self.cloud.preflight()
        self.cloud.mfa()
        self.cloud.identity(callback='https://' + self.private['hostname'] + '/oauth2/callback')
        self.guard_resource_identity()
        self.actual_infrastructure()
        require(not self.infrastructure_plan() and not self.budget_changed(), 'infrastructure-drift-detected')
        result = self.host('verify')
        self.public_verify()
        return {'status': result['status']}

    def expire_run_commands(self):
        values = self.cloud.arm(self.cloud.vm + '/runCommands').get('value', [])
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=self.settings['retention']['runCommandDays'])
        for value in values:
            if not value['name'].startswith('codex-infra-'): continue
            stamp = value.get('systemData', {}).get('lastModifiedAt')
            if stamp and dt.datetime.fromisoformat(stamp.replace('Z', '+00:00')) < cutoff:
                self.cloud.arm(value['id'], 'DELETE')

    def rollback(self, revision=None):
        self.cloud.preflight()
        self.cloud.mfa()
        self.cloud.backup(self.settings['backup']['dailyRetentionDays'])
        result = self.host('rollback', revision=revision)
        registration = self.cloud.identity()
        if registration['web']['redirectUris'] != [result['callback']]:
            self.cloud.graph('applications/' + self.private['runtimeApplicationObjectId'], 'PATCH',
                             {'web': {**registration['web'], 'redirectUris': [result['callback']]}})
        self.public_verify(hostname=result['hostname'])
        atomic(self.directory / 'rollback-result.json', result)
        return {'status': result['status'], 'affectedServices': result['affected']}

    def rotate_credentials(self):
        self.cloud.preflight(administrator=True)
        self.cloud.mfa()
        previous = copy.deepcopy(self.secrets)
        require(previous and previous.get('keyId'), 'previous-credential-mapping-required')
        expiry = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=180)
        new = self.cloud.graph('applications/' + self.private['runtimeApplicationObjectId'] + '/addPassword', 'POST',
                               {'passwordCredential': {'displayName': 'codex-terminal-runtime', 'endDateTime': expiry.isoformat()}})
        self.secrets = {**previous, 'clientSecret': new['secretText'], 'keyId': new['keyId'], 'expiresAt': expiry.isoformat()}
        atomic(self.directory / 'rotation-pending.json', {'previousKeyId': previous['keyId'], 'replacement': self.secrets})
        try:
            self.apply()
            self.public_verify()
            # Publish the verified secret before revoking the old credential. A failed
            # secret upload retains both credentials and the local recovery copy.
            command(['gh', 'secret', 'set', 'RUNTIME_SECRETS', '--repo', self.private['repository'], '--env', 'production'],
                    data=json.dumps(self.secrets), json_output=False)
            atomic(self.directory / 'runtime-secrets.json', self.secrets)
            self.cloud.graph('applications/' + self.private['runtimeApplicationObjectId'] + '/removePassword', 'POST',
                             {'keyId': previous['keyId']})
            (self.directory / 'rotation-pending.json').unlink()
            return {'status': 'credentials-rotated', 'affectedServices': ['auth']}
        except Exception:
            # The old credential is never revoked on failure. The pending recovery
            # copy identifies the new key without printing it.
            raise Failure('rotation-incomplete-old-credential-retained') from None
