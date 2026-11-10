import base64
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'operations'),str(ROOT/'tools')]
from cloud import Cloud, Failure, claims
from identity import oidc_subject
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

    def test_scanner_rejects_personal_inputs_history_credentials_and_public_ips(self):
        for data in [('gh'+'p_')+'a'*36,str(uuid.uuid4()),'person'+'@'+'private.invalid','203'+'.1.2.3']:
            with self.subTest(data='withheld'),self.assertRaises(ValueError):scan(data.encode(),[])
        with self.assertRaises(ValueError):scan(b'account-specific-domain', ['account-specific-domain'])
        scan(b'owner@example.com 127.0.0.1 169.254.169.254 168.63.129.16',[])

    def test_pr_workflows_never_request_production_identity_or_secrets(self):
        ci=(ROOT/'.github/workflows/ci.yml').read_text()
        self.assertNotIn('id-token:',ci);self.assertNotIn('secrets.',ci)
        self.assertNotIn('environment: production',ci);self.assertNotIn('pull_request_target',ci)
        production=(ROOT/'.github/workflows/production.yml').read_text()
        self.assertNotIn('pull_request',production)
        self.assertIn('cancel-in-progress: false',production)
        self.assertIn("github.ref == 'refs/heads/main'",production)
        self.assertIn('ref: main',production)
        self.assertNotIn('upload-artifact',production)
