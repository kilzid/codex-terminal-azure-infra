"""No-restart adoption with encrypted VM-to-administrator secret transfer."""
import base64
import datetime as dt
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile

from cloud import Failure, atomic, command, require
from identity import publish_secrets

EXPORT = r'''
import base64,json,os,pathlib,subprocess,tempfile,tomllib
p=json.loads(base64.b64decode(os.environ.pop('CODEX_VM_PAYLOAD')))
c=tomllib.load(open('/etc/codex-terminal/oauth2-proxy.cfg','rb'))
assert c['client_id']==p['client'] and c['allowed_groups']==[p['owner']]
assert c['oidc_issuer_url']=='https://login.microsoftonline.com/'+p['tenant']+'/v2.0'
with tempfile.TemporaryDirectory(prefix='codex-export-',dir='/run') as directory:
 certificate=pathlib.Path(directory)/'recipient.pem';certificate.write_text(p['certificate'])
 data=json.dumps({'clientSecret':c['client_secret'],'cookieSecret':c['cookie_secret']}).encode()
 result=subprocess.run(['openssl','cms','-encrypt','-aes-256-cbc','-outform','DER',str(certificate)],input=data,capture_output=True)
 assert result.returncode==0
 print('CODEX_RESULT='+base64.b64encode(json.dumps({'ciphertext':base64.b64encode(result.stdout).decode()}).encode()).decode())
'''


def import_runtime_secrets(ctl):
    p = ctl.private
    with tempfile.TemporaryDirectory(prefix='encrypted-transfer-', dir=ctl.directory) as directory:
        directory = Path(directory)
        key, cert = directory / 'recipient.key', directory / 'recipient.pem'
        command(['openssl', 'req', '-x509', '-newkey', 'rsa:3072', '-nodes', '-keyout', str(key), '-out', str(cert),
                 '-subj', '/CN=codex-bootstrap-transfer', '-days', '1'], json_output=False)
        key.chmod(0o600)
        result = ctl.cloud.run_host(EXPORT, {'certificate': cert.read_text(), 'client': p['runtimeClientId'],
            'owner': p['userObjectId'], 'tenant': p['tenantId']}, name='encrypted-adoption-export')
        ciphertext = directory / 'transfer.cms'
        ciphertext.write_bytes(base64.b64decode(result['ciphertext'], validate=True))
        decrypted = subprocess.run(['openssl', 'cms', '-decrypt', '-inform', 'DER', '-in', str(ciphertext),
                                   '-recip', str(cert), '-inkey', str(key)], capture_output=True)
        require(decrypted.returncode == 0, 'encrypted-transfer-decryption-failed')
        from controller import validate
        ctl.secrets = validate(json.loads(decrypted.stdout), 'runtime-secrets')
        app = ctl.cloud.graph('applications/' + p['runtimeApplicationObjectId'])
        credentials = app.get('passwordCredentials', [])
        # Preserve the mapped key. The original private state may hold several
        # historic credentials, so never guess which one can be revoked.
        key_file = ctl.directory / 'credential-mapping.json'
        if key_file.exists():
            mapping = json.loads(key_file.read_text())
            key_id = mapping['keyId']
            match = next((v for v in credentials if v['keyId'] == key_id), None)
            require(match, 'credential-key-mapping-invalid')
            ctl.secrets.update(keyId=key_id, expiresAt=match['endDateTime'])
        atomic(ctl.directory / 'runtime-secrets.json', ctl.secrets)


def adopt(ctl):
    ctl.cloud.preflight(administrator=True)
    ctl.cloud.mfa()
    require(ctl.private['profile'] == 'adopted', 'adoption-profile-required')
    ctl.cloud.identity(callback='https://' + ctl.private['hostname'] + '/oauth2/callback')
    if ctl.secrets is None: import_runtime_secrets(ctl)
    vm, ip = ctl.actual_infrastructure()
    plan = ctl.plan()
    management_additions = [n for n in plan['host']['changed'] if n == 'codex-config.py' and n not in plan['host']['before']]
    require(set(plan['host']['changed']) == set(management_additions) and not plan['host'].get('serviceDrift') and not plan['callbackChanged'] and not plan['budgetChanged'], 'adoption-would-change-live-configuration')
    require(not plan['infrastructure'], 'adoption-infrastructure-mismatch')
    ctl.public_verify()
    result = ctl.host('adopt')
    atomic(ctl.directory / 'adoption-baseline.json', {'vmId': vm['id'], 'instanceId': vm['properties']['vmId'],
        'diskId': vm['properties']['storageProfile']['osDisk']['managedDisk']['id'], 'ipId': ip['id'],
        'ipAddress': ip['properties']['ipAddress'], 'containers': result['containers'], 'commit': ctl.commit})
    publish_secrets(ctl.directory, ctl.private)
    return {'status': 'adopted', 'functionalChanges': 0, 'affectedServices': [],
            'managementFilesAdded': result.get('managementFilesAdded', [])}


def fresh(ctl, *, publish=True):
    ctl.cloud.preflight(administrator=True)
    ctl.cloud.mfa()
    p = ctl.private
    require(p['profile'] == 'deployment', 'fresh-profile-required')
    # Bootstrap authorizes creating the VM, but never replaces an existing one.
    from cloud import az
    exists = az('vm', 'list', '--resource-group', p['resourceGroup'], '--subscription', p['subscriptionId'])
    marker = ctl.directory / 'fresh-provisioning.json'
    require(not exists or marker.exists(), 'fresh-install-requires-empty-vm-inventory')
    require(all(v['name'] == p['resources']['vmName'] and v.get('tags', {}).get('application') == 'codex-terminal' for v in exists), 'fresh-resource-ownership-mismatch')
    if not exists:
        atomic(marker, {'resourceGroup': p['resourceGroup'], 'vmName': p['resources']['vmName']})
        ctl.deployment('create')
    vm = ctl.cloud.arm(ctl.cloud.vm)
    nic = ctl.cloud.arm(vm['properties']['networkProfile']['networkInterfaces'][0]['id'], api='2024-05-01')
    ip = ctl.cloud.arm(nic['properties']['ipConfigurations'][0]['properties']['publicIPAddress']['id'], api='2024-05-01')
    p['azureHostname'] = ip['properties']['dnsSettings']['fqdn']
    p.setdefault('hostname', p['azureHostname'])
    p['resources']['publicIp'] = ip['properties']['ipAddress']
    atomic(ctl.directory / 'private.json', p)
    if ctl.secrets is None:
        expiry = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=180)
        credential = ctl.cloud.graph('applications/' + p['runtimeApplicationObjectId'] + '/addPassword', 'POST',
            {'passwordCredential': {'displayName': 'codex-terminal-runtime', 'endDateTime': expiry.isoformat()}})
        ctl.secrets = {'clientSecret': credential['secretText'], 'cookieSecret': base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
                       'keyId': credential['keyId'], 'expiresAt': expiry.isoformat()}
        atomic(ctl.directory / 'runtime-secrets.json', ctl.secrets)
    ctl.cloud.graph('applications/' + p['runtimeApplicationObjectId'], 'PATCH',
                    {'web': {'redirectUris': ['https://' + p['hostname'] + '/oauth2/callback'],
                             'implicitGrantSettings': {'enableAccessTokenIssuance': False, 'enableIdTokenIssuance': False}}})
    plan = ctl.host('plan')
    result = ctl.host('apply', expectedBefore=plan['before'])
    import time
    deadline = time.monotonic() + 180
    while True:
        try:
            ctl.public_verify()
            break
        except (OSError, Failure):
            if time.monotonic() >= deadline: raise
            time.sleep(5)
    ctl.actual_infrastructure()
    ctl.host('verify')
    if publish: publish_secrets(ctl.directory, p)
    return {'status': 'fresh-install-complete', 'affectedServices': result['affected']}
