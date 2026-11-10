#!/usr/bin/env python3
"""Administrator-only isolated runtime failure and Azure Backup restoration test.

Never run against an adopted deployment. Output contains only fixed test statuses.
The --cleanup operation is separate from reconciliation and retains soft-delete
protection on the temporary vault. Operational IDs remain in the private folder.
"""
import argparse
import base64
import datetime as dt
import json
from pathlib import Path
import secrets
import sys
import time
import urllib.error
import uuid

from cloud import Failure, atomic, az, require
from controller import Controller, ROOT
from identity import local_policy_reader

MARKERS = r'''
import base64,json,os,subprocess
p=json.loads(base64.b64decode(os.environ.pop('CODEX_VM_PAYLOAD')))
script="""
const fs=require('node:fs');
const value=JSON.parse(process.argv[1]);
for(const file of ['/workspace/.infra-acceptance','/home/node/.codex/.infra-acceptance'])
 fs.writeFileSync(file,value.marker,{mode:0o600});
fs.writeFileSync('/home/node/.codex/acceptance-history.jsonl',JSON.stringify({text:value.marker})+'\\n',{mode:0o600});
"""
r=subprocess.run(['docker','compose','-f','/etc/codex-terminal/compose.json','exec','-T','terminal','node','-e',script,json.dumps(p)],capture_output=True)
assert r.returncode==0
print('CODEX_RESULT='+base64.b64encode(b'{"status":"markers-written"}').decode())
'''

RESTORE_CHECK = r'''
import base64,json,os,pathlib,subprocess,time
p=json.loads(base64.b64decode(os.environ.pop('CODEX_VM_PAYLOAD')))
mount=pathlib.Path('/mnt/codex-infra-acceptance');mount.mkdir(mode=0o700,exist_ok=True)
def run(args):
 r=subprocess.run(args,capture_output=True);assert r.returncode==0;return r.stdout
partition=pathlib.Path('/dev/disk/azure/scsi1/lun0-part1')
for attempt in range(60):
 if partition.exists():break
 time.sleep(1)
assert partition.exists()
kind=run(['blkid','-s','TYPE','-o','value',str(partition)]).decode().strip()
assert kind in ['ext4','xfs']
options='ro,nodev,nosuid,noexec,'+('noload' if kind=='ext4' else 'norecovery')
run(['mount','-o',options,str(partition),str(mount)])
try:
 for file in ['workspace/.infra-acceptance','codex-config/.infra-acceptance']:
  assert (mount/'srv/codex-terminal'/file).read_text()==p['marker']
 history=(mount/'srv/codex-terminal/codex-config/acceptance-history.jsonl').read_text()
 assert json.loads(history)['text']==p['marker']
 assert pathlib.Path('/srv/codex-terminal/workspace/.infra-acceptance').read_text()==p['newer']
finally:run(['umount',str(mount)])
print('CODEX_RESULT='+base64.b64encode(b'{"status":"isolated-backup-restoration-verified"}').decode())
'''

CHECK_LIVE = r'''
import base64,json,os,pathlib
p=json.loads(base64.b64decode(os.environ.pop('CODEX_VM_PAYLOAD')))
assert pathlib.Path('/srv/codex-terminal/workspace/.infra-acceptance').read_text()==p['marker']
assert pathlib.Path('/srv/codex-terminal/codex-config/.infra-acceptance').read_text()==p['marker']
print('CODEX_RESULT='+base64.b64encode(b'{"status":"newest-data-preserved"}').decode())
'''


def assert_isolated(ctl):
    ctl.cloud.preflight(administrator=True)
    require(ctl.private['profile'] == 'deployment', 'acceptance-forbids-adopted-deployment')
    p=ctl.private
    group=az('group','show','--name',p['resourceGroup'],'--subscription',p['subscriptionId'])
    require(group.get('tags',{}).get('purpose')=='codex-infra-acceptance', 'explicit-isolated-resource-group-tag-required')
    require(group.get('tags',{}).get('application')=='codex-terminal', 'test-resource-ownership-required')


def wait_job(ctl, job_id):
    p=ctl.private
    deadline=time.monotonic()+5400
    previous=None
    while time.monotonic()<deadline:
        job=az('backup','job','show','--resource-group',p['resourceGroup'],'--vault-name',p['resources']['vaultName'],
               '--name',job_id,'--subscription',p['subscriptionId'])
        status=job['properties']['status']
        if status=='Completed': return job
        require(status in ['Pending','InProgress'], 'isolated-backup-or-restore-job-failed')
        if status!=previous:
            print('{"status":"waiting-for-isolated-restore"}',flush=True);previous=status
        time.sleep(20)
    raise Failure('isolated-restore-timeout')


def test(ctl):
    assert_isolated(ctl)
    p=ctl.private
    ctl.verify()
    state_path=ctl.directory/'acceptance-state.json'
    state=json.loads(state_path.read_text()) if state_path.exists() else {}
    marker=state.get('marker') or secrets.token_hex(24)
    newer=marker+'-newer-than-backup'
    state.update(marker=marker,newer=newer)
    if not state.get('backupCompleted'):
        ctl.cloud.run_host(MARKERS,{'marker':marker},name='acceptance-markers')
        atomic(state_path,state)
        print('{"status":"isolated-backup-starting"}',flush=True)
        ctl.cloud.backup(ctl.settings['backup']['dailyRetentionDays'])
        state['backupCompleted']=True
    points=az('backup','recoverypoint','list','--resource-group',p['resourceGroup'],'--vault-name',p['resources']['vaultName'],
              '--container-name',p['resources']['backupContainer'],'--item-name',p['resources']['backupItem'],
              '--backup-management-type','AzureIaasVM','--subscription',p['subscriptionId'])
    require(points,'isolated-recovery-point-missing')
    point=next((v for v in points if v['name']==state.get('recoveryPoint')),None)
    point=point or max(points,key=lambda v:v['properties']['recoveryPointTime'])
    state['recoveryPoint']=point['name'];atomic(state_path,state)
    if not state.get('runtimeFailureRolledBack'):
        ctl.cloud.run_host(MARKERS,{'marker':newer},name='acceptance-newer-data')
        baseline=ctl.host('verify')['containers']
        # Substitute a different public binary only in this isolated host test.
        # It cannot pass the normal immutable lockfile schema or PR checks.
        payload=ctl.payload('apply')
        compose=json.loads(base64.b64decode(payload['files']['compose.json']))
        compose['services']['terminal']['image']=ctl.images['auth']
        payload['files']['compose.json']=base64.b64encode(json.dumps(compose).encode()).decode()
        payload['expectedBefore']=ctl.host('plan')['before']
        try:
            ctl.cloud.run_host((ROOT/'runtime/host.py').read_text(),payload,name='acceptance-failing-image')
        except Failure as error:
            require(str(error).startswith(('host-verify-', 'host-native-')), 'fault-did-not-reach-runtime-verification')
        else: raise Failure('deliberately-failing-image-was-accepted')
        after=ctl.host('verify')['containers']
        require(baseline['auth']==after['auth'] and baseline['https']==after['https'], 'fault-restarted-unaffected-services')
        ctl.cloud.run_host(CHECK_LIVE,{'marker':newer},name='acceptance-rollback-data')
        state['runtimeFailureRolledBack']=True;atomic(state_path,state)
        print('{"status":"failed-image-rollback-and-newest-data-preservation-verified"}',flush=True)
    restore_group=p['resourceGroup']+'-restore'
    if not state.get('restoreJob'):
        storage='ctrestore'+secrets.token_hex(6)
        tags=['application=codex-terminal','purpose=codex-infra-acceptance']
        az('group','create','--name',restore_group,'--location',p['location'],'--tags',*tags,'--subscription',p['subscriptionId'])
        account=az('storage','account','create','--resource-group',restore_group,'--name',storage,'--location',p['location'],
            '--sku','Standard_LRS','--kind','StorageV2','--min-tls-version','TLS1_2','--https-only','true',
            '--allow-blob-public-access','false','--default-action','Deny','--bypass','AzureServices','--tags',*tags,
            '--subscription',p['subscriptionId'])
        state.update(restoreGroup=restore_group,stagingStorage=account['id']);atomic(state_path,state)
        job=az('backup','restore','restore-disks','--resource-group',p['resourceGroup'],'--vault-name',p['resources']['vaultName'],
            '--container-name',p['resources']['backupContainer'],'--item-name',p['resources']['vmName'],'--rp-name',point['name'],
            '--storage-account',account['id'],'--storage-account-resource-group',restore_group,'--target-resource-group',restore_group,
            '--restore-only-osdisk','true','--restore-as-unmanaged-disks','false','--subscription',p['subscriptionId'])
        state['restoreJob']=job['name'];atomic(state_path,state)
    wait_job(ctl,state['restoreJob'])
    disks=az('disk','list','--resource-group',restore_group,'--subscription',p['subscriptionId'])
    require(len(disks)==1,'isolated-restore-disk-count-unexpected')
    state['restoredDisk']=disks[0]['id'];atomic(ctl.directory/'acceptance-state.json',state)
    az('vm','disk','attach','--resource-group',p['resourceGroup'],'--vm-name',p['resources']['vmName'],
       '--name',disks[0]['id'],'--lun','0','--subscription',p['subscriptionId'])
    try: ctl.cloud.run_host(RESTORE_CHECK,{'marker':marker,'newer':newer},name='acceptance-readonly-restore')
    finally:
        az('vm','disk','detach','--resource-group',p['resourceGroup'],'--vm-name',p['resources']['vmName'],
           '--name',disks[0]['name'],'--subscription',p['subscriptionId'])
    atomic(ctl.directory/'acceptance-evidence.json',{'runtimeFailureRolledBack':True,'newestDataPreserved':True,
        'unaffectedContainersPreserved':True,'backupRestoredIntoIsolatedGroup':True,'workspaceAndHistoryMarkersRestored':True,
        'commit':ctl.commit})
    print('{"status":"isolated-runtime-and-backup-restoration-tests-passed"}',flush=True)


def cleanup(ctl):
    assert_isolated(ctl)
    p=ctl.private
    state_path=ctl.directory/'acceptance-state.json'
    state=json.loads(state_path.read_text()) if state_path.exists() else {}
    # Explicit administrator cleanup; no routine deployment role has these rights.
    items=az('backup','item','list','--resource-group',p['resourceGroup'],'--vault-name',p['resources']['vaultName'],
             '--subscription',p['subscriptionId'])
    for item in items:
        require(item['properties']['sourceResourceId'].lower()==ctl.cloud.vm.lower(),'cleanup-backup-source-mismatch')
        az('backup','protection','disable','--resource-group',p['resourceGroup'],'--vault-name',p['resources']['vaultName'],
           '--container-name',p['resources']['vmName'],'--item-name',p['resources']['vmName'],
           '--backup-management-type','AzureIaasVM','--delete-backup-data','true','--yes','--subscription',p['subscriptionId'])
    vm=ctl.cloud.arm(ctl.cloud.vm)
    disk=vm['properties']['storageProfile']['osDisk']['managedDisk']['id']
    require(disk.lower().startswith(ctl.cloud.rg.lower()+'/'),'cleanup-disk-scope-mismatch')
    az('vm','delete','--ids',ctl.cloud.vm,'--yes','--subscription',p['subscriptionId'])
    az('disk','delete','--ids',disk,'--yes','--subscription',p['subscriptionId'])
    for kind,name in [('nic',p['prefix']+'-vm-nic'),('public-ip',p['prefix']+'-vm-ip'),
                      ('vnet',p['prefix']+'-vm-vnet'),('nsg',p['prefix']+'-vm-nsg')]:
        az('network',kind,'delete','--resource-group',p['resourceGroup'],'--name',name,'--subscription',p['subscriptionId'])
    if state.get('restoreGroup'):
        require(state['restoreGroup']==p['resourceGroup']+'-restore','cleanup-restore-scope-mismatch')
        group=az('group','show','--name',state['restoreGroup'],'--subscription',p['subscriptionId'])
        require(group.get('tags',{}).get('purpose')=='codex-infra-acceptance','cleanup-restore-tag-mismatch')
        az('group','delete','--name',state['restoreGroup'],'--yes','--subscription',p['subscriptionId'])
    # Soft-deleted recovery points/vault remain governed by the 14-day safety policy.
    atomic(ctl.directory/'cleanup-state.json',{'computeAndRestoredDisksRemoved':True,'softDeletedVaultRetained':True,
        'reviewAfter':(dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=15)).date().isoformat()})
    print('{"status":"isolated-compute-cleaned-backup-soft-delete-retained"}')


def cleanup_identity(ctl):
    """Remove only identities and role IDs created for this tagged test deployment."""
    assert_isolated(ctl)
    p=ctl.private
    require((ctl.directory/'cleanup-state.json').exists(), 'cleanup-compute-first')
    require(p['budgetName']==p['prefix']+'-monthly', 'cleanup-budget-name-mismatch')
    def optional(call, path, method='GET', **kwargs):
        try:return call(path,method,**kwargs)
        except urllib.error.HTTPError as error:
            if error.code != 404:raise
            return None
    # Validate both applications before removing any tenant object or permission.
    applications=[]
    for kind,suffix in [('runtime','-runtime'),('deployment','-github-deployment')]:
        path='applications/'+p[kind+'ApplicationObjectId']
        app=optional(ctl.cloud.graph,path)
        if app:
            require(app['appId']==p[kind+'ClientId'] and app['displayName']==p['prefix']+suffix,
                    'cleanup-application-identity-mismatch')
            applications.append(path)
    subscription='/subscriptions/'+p['subscriptionId']
    for suffix,scope in [('runtime',ctl.cloud.rg),('budget',ctl.budget_path)]:
        role_id=str(uuid.uuid5(uuid.NAMESPACE_URL,subscription+'/'+p['deploymentClientId']+'/'+suffix))
        definition=subscription+'/providers/Microsoft.Authorization/roleDefinitions/'+role_id
        assignment=scope+'/providers/Microsoft.Authorization/roleAssignments/'+str(uuid.uuid5(uuid.NAMESPACE_URL,scope+role_id+p['deploymentObjectId']))
        actual=optional(ctl.cloud.arm,assignment,api='2022-04-01')
        if actual:
            require(actual['properties']['principalId']==p['deploymentObjectId'] and
                    actual['properties']['roleDefinitionId'].lower()==definition.lower(), 'cleanup-role-identity-mismatch')
            optional(ctl.cloud.arm,assignment,'DELETE',api='2022-04-01')
        optional(ctl.cloud.arm,definition,'DELETE',api='2022-04-01')
    for path in applications:optional(ctl.cloud.graph,path,'DELETE')
    optional(ctl.cloud.arm,ctl.budget_path,'DELETE',api='2024-08-01')
    for kind,names in [('metricAlerts',[p['prefix']+'-vm-'+v for v in ['memory','credits','availability']]),
                       ('actionGroups',[p['prefix']+'-alerts'])]:
        for name in names:
            optional(ctl.cloud.arm,ctl.cloud.rg+'/providers/Microsoft.Insights/'+kind+'/'+name,'DELETE',api='2018-03-01' if kind=='metricAlerts' else '2023-01-01')
    state=json.loads((ctl.directory/'cleanup-state.json').read_text())
    state['testIdentitiesRolesBudgetAndMonitoringRemoved']=True
    atomic(ctl.directory/'cleanup-state.json',state)
    print('{"status":"isolated-test-identities-and-permissions-cleaned"}')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private',required=True)
    parser.add_argument('--cleanup',action='store_true')
    parser.add_argument('--cleanup-identity',action='store_true')
    args=parser.parse_args()
    ctl=Controller(args.private)
    require(not (args.cleanup and args.cleanup_identity),'choose-one-cleanup-operation')
    if args.cleanup_identity:cleanup_identity(ctl)
    elif args.cleanup:cleanup(ctl)
    else:
        with local_policy_reader(ctl.cloud):test(ctl)


if __name__=='__main__':
    try:main()
    except Failure as error:
        print(json.dumps({'status':'isolated-test-failed','code':str(error)}),file=sys.stderr);sys.exit(1)
    except Exception:
        print('{"status":"isolated-test-failed","code":"details-withheld"}',file=sys.stderr);sys.exit(1)
