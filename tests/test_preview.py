from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'operations'))
from preview import meaningful_changes


class PreviewTests(unittest.TestCase):
    def result(self,kind,delta):return {'changes':[{'changeType':'Modify','before':{'type':kind},'delta':delta}]}

    def test_unresolved_secure_parameters_are_compared_to_real_private_inputs(self):
        result=self.result('Microsoft.Storage/storageAccounts',[{'path':'tags.owner','propertyChangeType':'Modify',
             'before':'synthetic-owner', 'after':"[parameters('ownerObjectId')]"}])
        self.assertEqual(meaningful_changes(result,{'ownerObjectId':'synthetic-owner'}),[])
        self.assertEqual(len(meaningful_changes(result,{'ownerObjectId':'different-owner'})),1)

    def test_only_named_readonly_deletions_and_explicit_noeffects_are_ignored(self):
        result=self.result('Microsoft.Network/networkInterfaces',[
            {'path':'properties.allowPort25Out','propertyChangeType':'Delete','before':False},
            {'path':'properties.ipConfigurations','propertyChangeType':'Array','children':[
                {'path':'0','propertyChangeType':'Modify','children':[
                    {'path':'properties.privateIPAddress','propertyChangeType':'Delete','before':'10.0.0.4'}]}]}])
        self.assertEqual(meaningful_changes(result,{}),[])
        result['changes'][0]['delta'].append({'path':'properties.enableIPForwarding','propertyChangeType':'Modify','before':True,'after':False})
        self.assertEqual(len(meaningful_changes(result,{})),1)

    def test_unknown_changes_security_changes_and_resource_deletes_are_preserved(self):
        result=self.result('Microsoft.RecoveryServices/vaults',[{'path':'properties.securitySettings','propertyChangeType':'Delete','before':{'enabled':True}}])
        self.assertEqual(len(meaningful_changes(result,{})),1)
        self.assertEqual(len(meaningful_changes({'changes':[{'changeType':'Delete'}]},{})),1)
