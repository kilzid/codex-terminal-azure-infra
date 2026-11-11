import base64
import ast
import fnmatch
import json
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'operations'),str(ROOT/'tools')]
from cloud import Cloud, Failure, claims
from identity import oidc_subject, repository_binding
from privacy import scan


def jwt(value):return 'header.'+base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')+'.signature'


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.p={k:str(uuid.uuid4()) for k in ['tenantId','subscriptionId','adminObjectId','deploymentObjectId','deploymentClientId']}
        self.p.update(resourceGroup='rg-test',resources={'vmName':'test-vm'},repositoryId=1,repositoryOwnerId=2)

    def test_local_admin_rejects_wrong_tenant_subscription_and_identity(self):
        account={'id':self.p['subscriptionId'],'tenantId':self.p['tenantId'],'state':'Enabled',
                 'environmentName':'AzureCloud','user':{'type':'user'}}
        token=jwt({'tid':self.p['tenantId'],'oid':self.p['adminObjectId']})
        for field in ['id','tenantId']:
            with patch.dict(os.environ,{'GITHUB_ACTIONS':'false'}),patch('cloud.az',return_value={**account,field:'wrong'}):
                with self.assertRaises(Failure):Cloud(self.p).preflight()
        with patch.dict(os.environ,{'GITHUB_ACTIONS':'false'}),patch('cloud.az',side_effect=[account,{'accessToken':jwt({'tid':self.p['tenantId'],'oid':'wrong'})}]):
            with self.assertRaises(Failure):Cloud(self.p).preflight()

    def test_ci_rejects_other_repository_branch_tenant_subscription_and_client(self):
        env={'GITHUB_ACTIONS':'true','GITHUB_REF':'refs/heads/main','GITHUB_EVENT_NAME':'push',
             'GITHUB_REPOSITORY_ID':'1','GITHUB_REPOSITORY_OWNER_ID':'2'}
        account={'id':self.p['subscriptionId'],'tenantId':self.p['tenantId'],'state':'Enabled',
                 'environmentName':'AzureCloud','user':{'type':'servicePrincipal'}}
        for field in ['GITHUB_REF','GITHUB_EVENT_NAME','GITHUB_REPOSITORY_ID','GITHUB_REPOSITORY_OWNER_ID']:
            with patch.dict(os.environ,{**env,field:'wrong'}),patch('cloud.az',return_value=account):
                with self.assertRaises(Failure):Cloud(self.p).preflight()
        for field in ['tid','oid','appid']:
            token_claims={'tid':self.p['tenantId'],'oid':self.p['deploymentObjectId'],'appid':self.p['deploymentClientId']}
            token_claims[field]='wrong'
            with patch.dict(os.environ,env),patch('cloud.az',side_effect=[account,{'accessToken':jwt(token_claims)}]):
                with self.assertRaises(Failure):Cloud(self.p).preflight()
        for field in ['id','tenantId']:
            with patch.dict(os.environ,env),patch('cloud.az',return_value={**account,field:'wrong'}):
                with self.assertRaises(Failure):Cloud(self.p).preflight()

    def test_oidc_trust_binds_immutable_repository_environment_and_main(self):
        self.assertEqual(oidc_subject(1,2),'repository_id:1:repository_owner_id:2:environment:production:ref:refs/heads/main')

    def test_runtime_role_covers_every_declared_infrastructure_resource_write(self):
        tree=ast.parse((ROOT/'operations/identity.py').read_text())
        grant=next(node for node in ast.walk(tree) if isinstance(node,ast.Call) and
                   isinstance(node.func,ast.Name) and node.func.id=='grant_role' and
                   len(node.args)==5 and ast.literal_eval(node.args[3])=='runtime')
        actions=ast.literal_eval(grant.args[4])
        for template in (ROOT/'infra').glob('*.bicep'):
            for resource in re.finditer(r"resource\s+\w+\s+'([^'@]+)@[^']+'\s*(existing)?",template.read_text()):
                if resource[2]:continue
                required=resource[1]+'/write'
                with self.subTest(resource=required):
                    self.assertTrue(any(fnmatch.fnmatchcase(required.lower(),action.lower()) for action in actions),required)

    def test_scanner_rejects_personal_inputs_history_credentials_and_public_ips(self):
        for data in [('gh'+'p_')+'a'*36,str(uuid.uuid4()),'person'+'@'+'private.invalid','203'+'.1.2.3']:
            with self.subTest(data='withheld'),self.assertRaises(ValueError):scan(data.encode(),[])
        with self.assertRaises(ValueError):scan(b'account-specific-domain', ['account-specific-domain'])
        scan(b'owner@example.com 127.0.0.1 169.254.169.254 168.63.129.16',[])

    def test_owner_email_exception_does_not_allow_other_personal_addresses(self):
        scan(b'Author: kilzid <kilzid@gmail.com>', [])
        for value in [b'other'+b'@gmail.com', b'kilzid+other'+b'@gmail.com', b'kilzid'+b'@gmail.com.invalid']:
            with self.assertRaises(ValueError): scan(value, [])
        with self.assertRaises(ValueError): scan(b'kilzid@gmail.com', ['kilzid@gmail.com'])

    def test_pr_workflows_never_request_production_identity_or_secrets(self):
        ci=(ROOT/'.github/workflows/ci.yml').read_text()
        self.assertNotIn('id-token:',ci);self.assertNotIn('secrets.',ci)
        self.assertNotIn('environment: production',ci);self.assertNotIn('pull_request_target',ci)
        production=(ROOT/'.github/workflows/production.yml').read_text()
        self.assertNotIn('pull_request',production)
        self.assertIn('cancel-in-progress: false',production)
        self.assertIn('queue: max',production)
        self.assertIn("github.ref == 'refs/heads/main'",production)
        self.assertIn('ref: main',production)
        self.assertNotIn('upload-artifact',production)

    def test_forks_can_bootstrap_their_own_identity_without_inheriting_upstream(self):
        repo={'full_name':'example/infra','fork':True,'id':3,'owner':{'id':4},'permissions':{'admin':True}}
        self.assertEqual(repository_binding(repo,'example/infra'),{'repositoryId':3,'repositoryOwnerId':4})
        with self.assertRaises(Failure):repository_binding(repo,'another/infra')
        repo['permissions']['admin']=False
        with self.assertRaises(Failure):repository_binding(repo,'example/infra')


class CheckedRevisionHistoryTests(unittest.TestCase):
    def test_full_checked_revision_history_excludes_unmerged_branch_files(self):
        import contextlib
        import io
        import subprocess
        import tempfile
        import privacy
        def git(directory, *args):
            return subprocess.check_output(['git', '-C', str(directory), *args], stderr=subprocess.PIPE).decode().strip()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root/'source'; source.mkdir()
            git(source, 'init', '-b', 'main')
            git(source, 'config', 'user.name', 'Test')
            git(source, 'config', 'user.email', 'noreply@github.com')
            (source/'README.md').write_text('public baseline\n')
            (source/'RELEASE_FILES.json').write_text(json.dumps(['README.md', 'RELEASE_FILES.json']))
            git(source, 'add', '.'); git(source, 'commit', '-m', 'Baseline')
            (source/'README.md').write_text('public revision\n')
            git(source, 'commit', '-am', 'Reviewed revision')
            git(source, 'switch', '-c', 'feature/future')
            (source/'future.txt').write_text('public future feature\n')
            (source/'RELEASE_FILES.json').write_text(json.dumps(['README.md', 'RELEASE_FILES.json', 'future.txt']))
            git(source, 'add', '.'); git(source, 'commit', '-m', 'Unmerged feature')
            clone = root/'checked'
            subprocess.check_call(['git', 'clone', '--depth=1', '--single-branch', '--no-tags', '--branch', 'main',
                                   source.as_uri(), str(clone)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            head = git(clone, 'rev-parse', 'HEAD')
            git(clone, 'fetch', '--no-tags', '--unshallow', 'origin', head)
            self.assertEqual(git(clone, 'rev-list', '--count', 'HEAD'), '2', 'full checked revision history is retained')
            with patch.object(privacy, 'ROOT', clone), patch.object(sys, 'argv', ['privacy.py']), contextlib.redirect_stdout(io.StringIO()):
                privacy.main()
                # Reproduce the old fetch-depth:0 behavior that also fetched the future branch.
                git(clone, 'fetch', 'origin', '+refs/heads/*:refs/remotes/origin/*')
                with self.assertRaisesRegex(ValueError, 'historic-file-outside-whitelist'):
                    privacy.main()
