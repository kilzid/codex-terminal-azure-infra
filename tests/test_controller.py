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

    def test_backup_failure_prevents_infrastructure_callback_and_runtime_mutation(self):
        c=self.controller()
        c.plan=Mock(return_value={'infrastructure':[{'changeType':'Modify'}],
            'host':{'applied':{'commit':'synthetic'},'changed':['compose.json'],'before':{}},
            'budgetChanged':False,'callbackChanged':False})
        c.host=Mock();c.deployment=Mock();c.cloud.backup.side_effect=Failure('backup-failed')
        with self.assertRaises(Failure):c.apply()
        c.host.assert_not_called();c.deployment.assert_not_called();c.cloud.graph.assert_not_called()

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
        c.cloud.backup.assert_called_once()

    def test_failed_resize_restores_previous_size_and_starts_vm(self):
        c=self.controller();c.settings['vmSize']='Standard_B2als_v2'
        c.cloud.vm='synthetic-vm-id'
        c.cloud.arm.return_value={'properties':{'hardwareProfile':{'vmSize':'Standard_B1ms'}}}
        c.deployment=Mock(side_effect=Failure('infrastructure-failed'))
        with patch('controller.az') as az:
            with self.assertRaises(Failure):c.apply_infrastructure()
        self.assertEqual([call.args[1] for call in az.call_args_list],['deallocate','resize','start'])
        self.assertIn('Standard_B1ms',az.call_args_list[1].args)
