import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'operations'))
from controller import Controller
from cloud import Failure
from acceptance import assert_isolated, cleanup_identity


class AcceptanceCleanupTests(unittest.TestCase):
    def test_cleanup_rejects_production_before_cloud_mutation(self):
        ctl=Mock(private={'profile':'adopted'})
        with self.assertRaisesRegex(Failure,'acceptance-forbids-adopted-deployment'):
            assert_isolated(ctl)
        ctl.cloud.arm.assert_not_called();ctl.cloud.graph.assert_not_called()

    def test_cleanup_checks_both_application_bindings_before_deleting_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory);(path/'cleanup-state.json').write_text('{}')
            ctl=Mock(directory=path,private={'prefix':'test','budgetName':'test-monthly',
                'runtimeApplicationObjectId':'runtime-object','runtimeClientId':'runtime-client',
                'deploymentApplicationObjectId':'deployment-object','deploymentClientId':'deployment-client'})
            ctl.cloud.graph.side_effect=[{'appId':'runtime-client','displayName':'test-runtime'},
                                        {'appId':'different-client','displayName':'test-github-deployment'}]
            with patch('acceptance.assert_isolated'),self.assertRaisesRegex(Failure,'cleanup-application-identity-mismatch'):
                cleanup_identity(ctl)
            ctl.cloud.arm.assert_not_called()
            self.assertTrue(all(call.args[1:]==('GET',) for call in ctl.cloud.graph.call_args_list))


class ControllerTests(unittest.TestCase):
    def controller(self):
        c=object.__new__(Controller)
        c.private={'resourceGroup':'rg-test','subscriptionId':'synthetic-subscription',
                   'hostname':'terminal.example.com','runtimeApplicationObjectId':'synthetic-application','profile':'adopted'}
        c.settings={'backup':{'dailyRetentionDays':7}}
        c.cloud=Mock()
        c.public_verify=Mock();c.actual_infrastructure=Mock();c.expire_run_commands=Mock()
        return c

    def test_arm_preview_is_machine_readable_and_temporary_file_is_deleted(self):
        c=self.controller();c.parameters=Mock(return_value={'location':'northeurope'})
        with tempfile.TemporaryDirectory() as directory:
            c.directory=Path(directory)
            with patch('controller.az',return_value={'status':'Succeeded','changes':[]}) as az:
                c.deployment('what-if')
                self.assertIn('--no-pretty-print',az.call_args.args)
                param=next(v for v in az.call_args.args if v.startswith('@'))
                self.assertFalse(Path(param[1:]).exists())
            self.assertEqual(json.loads((c.directory/'what-if.json').read_text())['status'],'Succeeded')

    def test_noop_apply_neither_backs_up_nor_modifies_infrastructure_or_identity(self):
        c=self.controller()
        c.plan=Mock(return_value={'infrastructure':[],'host':{'applied':{'commit':'synthetic'},'changed':[],'before':{}},
                                 'budgetChanged':False,'callbackChanged':False})
        c.host=Mock(return_value={'status':'unchanged','changed':[],'affected':[]})
        c.cloud.identity.return_value={'web':{'redirectUris':['https://terminal.example.com/oauth2/callback']}}
        c.deployment=Mock()
        self.assertEqual(c.apply()['status'],'unchanged')
        c.cloud.backup.assert_not_called();c.deployment.assert_not_called()
        c.cloud.graph.assert_not_called();c.cloud.arm.assert_not_called()

    def test_terminal_image_update_uses_daily_backup_and_still_verifies_and_commits(self):
        c=self.controller()
        c.plan=Mock(return_value={'infrastructure':[],
            'host':{'applied':{'commit':'synthetic'},'changed':['compose.json'],'before':{},'terminalImageOnly':True},
            'budgetChanged':False,'callbackChanged':False})
        c.host=Mock(return_value={'status':'applied','changed':['compose.json'],'affected':['terminal']})
        c.cloud.identity.return_value={'web':{'redirectUris':['https://terminal.example.com/oauth2/callback']}}
        result=c.apply()
        self.assertEqual(result['affectedServices'],['terminal'])
        c.cloud.backup.assert_not_called()
        c.public_verify.assert_called_once()
        self.assertEqual([v.args[0] for v in c.host.call_args_list],['apply','commit'])

    def test_all_deployment_change_types_skip_on_demand_backup_and_still_verify(self):
        for infrastructure, callback, changed, drift in [
                ([{'changeType': 'Modify'}], False, ['compose.json'], []),
                ([], True, ['compose.json'], []),
                ([], False, ['session-manager.mjs'], []),
                ([], False, [], ['terminal'])]:
            with self.subTest(infrastructure=infrastructure, callback=callback, changed=changed, drift=drift):
                c = self.controller()
                c.plan = Mock(return_value={'infrastructure': infrastructure,
                    'host': {'applied': {'commit': 'synthetic'}, 'changed': changed, 'before': {}, 'serviceDrift': drift},
                    'budgetChanged': False, 'callbackChanged': callback})
                c.host = Mock(return_value={'status': 'applied', 'changed': changed, 'affected': ['terminal']})
                c.apply_infrastructure = Mock()
                c.cloud.backup.side_effect = Failure('backup-must-not-be-called')
                c.cloud.identity.return_value = {'web': {'redirectUris': ['https://terminal.example.com/oauth2/callback']}}
                self.assertEqual(c.apply()['status'], 'applied')
                c.cloud.backup.assert_not_called()
                c.public_verify.assert_called_once()
                self.assertEqual([call.args[0] for call in c.host.call_args_list], ['apply', 'commit'])
                self.assertEqual(c.apply_infrastructure.call_count, int(bool(infrastructure)))

    def test_explicit_runtime_rollback_does_not_wait_for_a_backup(self):
        c = self.controller()
        c.cloud.backup.side_effect = Failure('backup-must-not-be-called')
        c.cloud.identity.return_value = {'web': {'redirectUris': ['https://terminal.example.com/oauth2/callback']}}
        c.host = Mock(return_value={'status': 'rolled-back', 'affected': ['terminal'],
            'hostname': 'terminal.example.com', 'callback': 'https://terminal.example.com/oauth2/callback'})
        with tempfile.TemporaryDirectory() as directory:
            c.directory = Path(directory)
            self.assertEqual(c.rollback()['status'], 'rolled-back')
        c.cloud.backup.assert_not_called()
        c.public_verify.assert_called_once_with(hostname='terminal.example.com')

    def test_public_health_failure_rolls_back_runtime_and_callback(self):
        c=self.controller();c.plan=Mock(return_value={'infrastructure':[],
            'host':{'applied':{'commit':'synthetic'},'changed':['Caddyfile'],'before':{}},
            'budgetChanged':False,'callbackChanged':True})
        previous={'redirectUris':['https://previous.example.com/oauth2/callback']}
        c.cloud.identity.return_value={'web':previous}
        c.host=Mock(return_value={'status':'applied','revision':'synthetic-revision'})
        c.public_verify.side_effect=Failure('public-entra-boundary-failed')
        with self.assertRaises(Failure):c.apply()
        self.assertEqual([v.args[0] for v in c.host.call_args_list],['apply','rollback'])
        self.assertEqual(c.cloud.graph.call_args.args[-1],{'web':previous})
        c.cloud.backup.assert_not_called()

    def test_failed_resize_restores_previous_size_and_starts_vm(self):
        c=self.controller();c.settings['vmSize']='Standard_B2als_v2'
        c.cloud.vm='synthetic-vm-id'
        c.cloud.arm.return_value={'properties':{'hardwareProfile':{'vmSize':'Standard_B1ms'}}}
        c.deployment=Mock(side_effect=Failure('infrastructure-failed'))
        with patch('controller.az') as az:
            with self.assertRaises(Failure):c.apply_infrastructure()
        self.assertEqual([call.args[1] for call in az.call_args_list],['deallocate','resize','start'])
        self.assertIn('Standard_B1ms',az.call_args_list[1].args)

    def test_bound_resource_identity_rejects_recreated_vm_before_preview(self):
        c=self.controller()
        c.private['resources']={'vmInstanceId':'original-instance','osDiskId':'synthetic-disk',
            'osDiskUniqueId':'original-disk-instance','publicIpId':'synthetic-ip','publicIp':'192.0.2.1'}
        c.cloud.arm.return_value={'properties':{'vmId':'recreated-instance'}}
        with self.assertRaises(Failure):c.guard_resource_identity()
        self.assertEqual(c.cloud.arm.call_count,1)

    def test_bound_resource_identity_rejects_recreated_disk_with_same_resource_id(self):
        c=self.controller()
        c.private['resources']={'vmInstanceId':'original-instance','osDiskId':'synthetic-disk',
            'osDiskUniqueId':'original-disk-instance','publicIpId':'synthetic-ip','publicIp':'192.0.2.1'}
        c.cloud.arm.side_effect=[{'properties':{'vmId':'original-instance',
            'storageProfile':{'osDisk':{'managedDisk':{'id':'synthetic-disk'}}}}},
            {'properties':{'uniqueId':'replacement-disk-instance'}}]
        with self.assertRaises(Failure):c.guard_resource_identity()
        self.assertEqual(c.cloud.arm.call_count,2)
