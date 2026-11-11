import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock
spec = importlib.util.spec_from_file_location('cancellation', Path(__file__).resolve().parents[1] / 'tools/cancel-obsolete-production.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


class CancellationTests(unittest.TestCase):
    def setUp(self):
        self.run = {'id': 42, 'path': '.github/workflows/production.yml', 'name': 'Production',
                    'head_branch': 'main', 'status': 'in_progress', 'head_sha': 'older'}

    def test_only_the_explicit_unchanged_ancestor_production_run_can_be_cancelled(self):
        api = Mock(side_effect=[{'object': {'sha': 'current'}}, self.run,
                   {'status': 'ahead'}, self.run, {'object': {'sha': 'current'}}, None])
        self.assertEqual(c.cancel(api, 42), {'status': 'obsolete-production-cancellation-requested', 'run': 42})
        api.assert_called_with('/actions/runs/42/cancel', 'POST')

    def test_current_revision_and_other_workflows_are_left_alone(self):
        for change in [{'head_sha': 'current'}, {'name': 'Validate'}, {'head_branch': 'feature/test'}]:
            with self.subTest(change=change):
                api = Mock(side_effect=[{'object': {'sha': 'current'}}, {**self.run, **change}])
                with self.assertRaises(c.u.UpdateError): c.cancel(api, 42)
                self.assertTrue(all(len(call.args) == 1 for call in api.call_args_list))

    def test_already_cancelled_run_is_a_successful_noop(self):
        api = Mock(side_effect=[{'object': {'sha': 'current'}}, {**self.run, 'status': 'completed'}])
        self.assertEqual(c.cancel(api, 42)['status'], 'production-run-already-completed')
        self.assertTrue(all(len(call.args) == 1 for call in api.call_args_list))

    def test_manual_cancellation_winning_the_race_is_a_successful_noop(self):
        api = Mock(side_effect=[{'object': {'sha': 'current'}}, self.run,
                   {'status': 'ahead'}, self.run, {'object': {'sha': 'current'}}, c.u.UpdateError('github-api-409'),
                   {**self.run, 'status': 'completed'}])
        self.assertEqual(c.cancel(api, 42)['status'], 'production-run-already-completed')

    def test_diverged_run_is_never_cancelled(self):
        api = Mock(side_effect=[{'object': {'sha': 'current'}}, self.run, {'status': 'diverged'}])
        with self.assertRaises(c.u.UpdateError): c.cancel(api, 42)
        self.assertTrue(all(len(call.args) == 1 for call in api.call_args_list))
