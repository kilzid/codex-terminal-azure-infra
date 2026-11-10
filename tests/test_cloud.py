import base64
import json
import gzip
from pathlib import Path
import sys
import unittest
import tempfile
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'operations'))
from cloud import Cloud


class RunCommandTests(unittest.TestCase):
    def test_stale_completed_run_command_is_not_accepted_for_new_invocation(self):
        cloud=Cloud({'subscriptionId':'synthetic-subscription','resourceGroup':'rg-test',
                     'location':'northeurope','resources':{'vmName':'test-vm'}})
        cloud.arm=Mock(side_effect=[{'value':[]},{}]);cloud.token=Mock(return_value='synthetic-token')
        result=base64.b64encode(json.dumps({'status':'fresh'}).encode()).decode()
        old={'properties':{'instanceView':{'executionState':'Succeeded','exitCode':0,
             'output':'CODEX_INVOCATION=older\nCODEX_RESULT='+result}}}
        fresh={'properties':{'instanceView':{'executionState':'Succeeded','exitCode':0,
               'output':'CODEX_INVOCATION=new-invocation\nCODEX_RESULT='+result}}}
        with patch('cloud.secrets.token_hex',return_value='new-invocation'), \
             patch('cloud.request',side_effect=[old,fresh]) as request,patch('cloud.time.sleep'):
            self.assertEqual(cloud.run_host('print("public protocol")',{'private':'synthetic-value'}),{'status':'fresh'})
        self.assertEqual(request.call_count,2)
        body=cloud.arm.call_args.args[2]
        self.assertIn('new-invocation',body['properties']['source']['script'])
        self.assertNotIn('synthetic-value',body['properties']['source']['script'])
        self.assertEqual(body['properties']['parameters'],[])
        protected=body['properties']['protectedParameters'][0]
        self.assertEqual(protected['name'],'CODEX_VM_PAYLOAD')
        self.assertEqual(json.loads(gzip.decompress(base64.b64decode(protected['value']))),{'private':'synthetic-value'})

    def test_existing_owned_command_is_reused_for_different_operations(self):
        cloud=Cloud({'subscriptionId':'synthetic-subscription','resourceGroup':'rg-test',
                     'location':'northeurope','resources':{'vmName':'test-vm'}})
        existing=cloud.vm+'/runCommands/codex-infra-plan'
        cloud.arm=Mock(side_effect=[{'value':[{'id':existing}]},{}])
        cloud.token=Mock(return_value='synthetic-token')
        result=base64.b64encode(b'{"status":"fresh"}').decode()
        fresh={'properties':{'instanceView':{'executionState':'Succeeded','exitCode':0,
               'output':'CODEX_INVOCATION=new-invocation\nCODEX_RESULT='+result}}}
        with patch('cloud.secrets.token_hex',return_value='new-invocation'),patch('cloud.request',return_value=fresh):
            cloud.run_host('print("public protocol")',{},name='verify')
        self.assertEqual(cloud.arm.call_args.args[0],existing)
        self.assertFalse(cloud.arm.call_args.args[2]['properties']['treatFailureAsDeploymentFailure'])

    def test_capacity_cleanup_archives_owned_records_before_deleting_and_preserves_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            cloud=Cloud({'subscriptionId':'synthetic-subscription','resourceGroup':'rg-test',
                         'location':'northeurope','resources':{'vmName':'test-vm'}},directory)
            legacy=[{'id':cloud.vm+'/runCommands/legacy-'+str(i)} for i in range(21)]
            owned=[{'id':cloud.vm+'/runCommands/codex-infra-'+name} for name in ['plan','adopt','export','diagnostic']]
            deleted=[]
            def arm(path,method='GET',body=None):
                if method=='GET':return {'value':legacy+owned}
                if method=='DELETE':
                    archives=list((Path(directory)/'run-command-archives').glob('*.json'))
                    self.assertEqual(len(archives),1)
                    self.assertEqual(len(json.loads(archives[0].read_text())['records']),3)
                    deleted.append(path)
                return {}
            cloud.arm=Mock(side_effect=arm);cloud.token=Mock(return_value='synthetic-token')
            result=base64.b64encode(b'{"status":"fresh"}').decode()
            fresh={'properties':{'instanceView':{'executionState':'Succeeded','exitCode':0,
                   'output':'CODEX_INVOCATION=new-invocation\nCODEX_RESULT='+result}}}
            with patch('cloud.secrets.token_hex',return_value='new-invocation'),patch('cloud.request',return_value=fresh):
                cloud.run_host('print("public protocol")',{})
            self.assertEqual(set(deleted),{v['id'] for v in owned[1:]})
            self.assertEqual(cloud.command_path,owned[0]['id'])
            protected=cloud.arm.call_args.args[2]['properties']['protectedParameters'][0]['value']
            decoded=json.loads(gzip.decompress(base64.b64decode(protected)))
            self.assertEqual(len(decoded['_runCommandArchive']['records']),3)


class CommandDiagnosticsTests(unittest.TestCase):
    def test_failed_commands_identify_stage_without_exposing_private_output(self):
        from cloud import command, Failure
        operations = [(['az', 'backup', 'protection', 'backup-now'], 'azure-backup-trigger-failed'),
                      (['az', 'backup', 'job', 'show'], 'azure-backup-status-failed'),
                      (['az', 'deployment', 'group', 'create'], 'azure-infrastructure-deployment-failed'),
                      (['az', 'vm', 'start'], 'azure-vm-start-failed'),
                      (['az', 'account', 'show'], 'azure-command-failed'),
                      (['unknown-command'], 'external-command-failed')]
        for args, code in operations:
            with self.subTest(args=args), patch('cloud.subprocess.run', return_value=Mock(
                    returncode=1, stdout='private output', stderr='private details')):
                with self.assertRaisesRegex(Failure, '^' + code + '$'):
                    command(args + ['--private-value', 'synthetic-account'])

    def test_authorization_errors_are_classified_without_printing_resource_ids(self):
        from cloud import command, Failure
        with patch('cloud.subprocess.run', return_value=Mock(returncode=1, stdout='',
                stderr='ERROR: (AuthorizationFailed) private account and resource details')):
            with self.assertRaisesRegex(Failure, '^azure-backup-trigger-authorization-failed$'):
                command(['az', 'backup', 'protection', 'backup-now'])


class WorkloadRefreshTests(unittest.TestCase):
    def setUp(self):
        self.private = {'repositoryId': 1, 'repositoryOwnerId': 2, 'deploymentClientId': 'synthetic-client',
                        'tenantId': 'synthetic-tenant', 'subscriptionId': 'synthetic-subscription',
                        'resourceGroup': 'synthetic-group', 'resources': {'vaultName': 'synthetic-vault',
                        'backupContainer': 'synthetic-container', 'backupItem': 'synthetic-item'}}
        self.env = {'GITHUB_ACTIONS': 'true', 'ACTIONS_ID_TOKEN_REQUEST_URL':
                    'https://pipelines.actions.githubusercontent.com/idtoken?audience=old',
                    'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'synthetic-bearer'}
        self.identity = {'iss': 'https://token.actions.githubusercontent.com', 'aud': 'api://AzureADTokenExchange',
                         'sub': 'repository_id:1:repository_owner_id:2:environment:production:ref:refs/heads/main'}
        def token(c):
            part = base64.urlsafe_b64encode(json.dumps(c).encode()).decode().rstrip('=')
            return 'header.' + part + '.signature'
        self.assertion = token(self.identity)

    def test_expired_assertion_has_a_specific_public_code(self):
        from cloud import command, Failure
        with patch('cloud.subprocess.run', return_value=Mock(returncode=1, stdout='',
                stderr='AADSTS700024: private assertion and account details')):
            with self.assertRaisesRegex(Failure, '^azure-command-assertion-expired$'):
                command(['az', 'account', 'get-access-token'])

    def test_oidc_is_reacquired_after_interval_and_cached_audience_tokens_are_cleared(self):
        cloud = Cloud(self.private)
        with patch.dict('os.environ', self.env), patch('cloud.request', return_value={'value': self.assertion}) as req, \
             patch('cloud.az') as az, patch('cloud.time.monotonic', side_effect=[0, 299, 301, 302]):
            cloud.refresh_workload_login()
            cloud.tokens['cached'] = 'old-access-token'
            cloud.refresh_workload_login()
            self.assertEqual(cloud.tokens['cached'], 'old-access-token')
            cloud.refresh_workload_login()
            self.assertEqual(cloud.tokens, {})
            cloud.refresh_workload_login(force=True)
        self.assertEqual(req.call_count, 3)
        self.assertEqual(az.call_count, 3)
        self.assertTrue(req.call_args.args[0].endswith('audience=api%3A%2F%2FAzureADTokenExchange'))
        self.assertEqual(req.call_args.kwargs, {'token': 'synthetic-bearer'})
        self.assertEqual(az.call_args.args[:3], ('login', '--service-principal', '--username'))
        self.assertIn(self.assertion, az.call_args.args)

    def test_other_identity_and_untrusted_token_endpoint_are_rejected_before_login(self):
        from cloud import Failure
        cloud = Cloud(self.private)
        with patch.dict('os.environ', self.env), patch('cloud.request', return_value={'value': self.assertion}), \
             patch('cloud.claims', return_value={**self.identity, 'sub': 'other-repository'}), patch('cloud.az') as az:
            with self.assertRaisesRegex(Failure, 'github-oidc-identity-mismatch'): cloud.refresh_workload_login()
            az.assert_not_called()
        with patch.dict('os.environ', {**self.env, 'ACTIONS_ID_TOKEN_REQUEST_URL': 'https://example.com/token'}), \
             patch('cloud.request') as req:
            with self.assertRaisesRegex(Failure, 'github-oidc-request-unavailable'): cloud.refresh_workload_login()
            req.assert_not_called()

    def test_local_administrator_does_not_use_github_login(self):
        with patch.dict('os.environ', {'GITHUB_ACTIONS': 'false'}), patch('cloud.request') as req, patch('cloud.az') as az:
            Cloud(self.private).refresh_workload_login(force=True)
            req.assert_not_called(); az.assert_not_called()

    def test_backup_refreshes_during_polling_and_before_followup_operations(self):
        cloud = Cloud(self.private)
        cloud.refresh_workload_login = Mock()
        with patch('cloud.az', side_effect=[{'name': 'synthetic-job'},
                {'properties': {'status': 'InProgress'}}, {'properties': {'status': 'Completed'}}]), patch('cloud.time.sleep'):
            cloud.backup(7)
        self.assertEqual(cloud.refresh_workload_login.call_count, 4)
        cloud.refresh_workload_login.assert_called_with(force=True)
