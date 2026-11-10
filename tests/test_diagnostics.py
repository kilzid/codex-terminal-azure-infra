import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'operations'))
from diagnostics import diagnose, errors


class DiagnosticsTests(unittest.TestCase):
    def test_only_public_states_and_error_codes_leave_read_only_requests(self):
        cloud = Mock(rg='/subscriptions/synthetic/resourceGroups/private-group',
                     private={'resources': {'vaultName': 'private-vault'}})
        vault = cloud.rg + '/providers/Microsoft.RecoveryServices/vaults/private-vault'
        cloud.arm.side_effect = [
            {'value': [{'id': vault + '/backupJobs/private-job', 'properties': {'startTime': '2026-11-10T20:00:00Z'}}]},
            {'properties': {'status': 'Failed', 'startTime': '2026-11-10T20:00:00Z', 'entityFriendlyName': 'private-vm',
                            'errorDetails': [{'errorCode': 123, 'errorString': 'private resource details'}]}},
            {'properties': {'provisioningState': 'Failed', 'error': {'code': 'DeploymentFailed', 'message': 'private deployment'}}},
            {'value': [{'properties': {'provisioningState': 'Failed', 'statusMessage': json.dumps({
                'error': {'code': 'AuthorizationFailed', 'message': 'private principal'}})}}]}]
        result = diagnose(cloud)
        self.assertNotIn('private', json.dumps(result))
        self.assertEqual(result['backupJobs'][0]['errorCodes'], [123])
        self.assertEqual(result['deployment']['unfinishedOperations'][0]['errorCodes'], ['AuthorizationFailed'])
        self.assertTrue(all(len(call.args) == 1 for call in cloud.arm.call_args_list), 'all requests use default GET')

    def test_error_messages_and_invalid_codes_are_not_output(self):
        self.assertEqual(errors({'code': '/subscriptions/private', 'message': 'private', 'details': [
            {'code': 'AuthorizationFailed', 'message': 'private'}]}), ['AuthorizationFailed'])

    def test_diagnostic_identity_is_restricted_to_main_and_not_pr_events(self):
        workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/diagnostics.yml').read_text()
        self.assertIn("github.ref == 'refs/heads/main'", workflow)
        self.assertIn('environment: production', workflow)
        self.assertNotIn('pull_request', workflow)
        self.assertNotIn('contents: write', workflow)
