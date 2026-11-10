import copy
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('image_update', ROOT/'tools/update-terminal.py')
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)


class ImageUpdateTests(unittest.TestCase):
    def setUp(self):
        self.lock = json.loads((ROOT/'images.lock.json').read_text())
        self.digest = 'sha256:' + 'f'*64
        self.updated = u.replace_terminal(self.lock, self.digest)
        self.repo = 'example/infra'
        self.branch = u.PREFIX + 'f'*16 + '-base'
        self.pr = {'number': 7, 'user': {'login': u.BOT['name']}, 'state': 'open', 'draft': False,
                   'head': {'ref': self.branch, 'sha': 'head', 'repo': {'full_name': self.repo}},
                   'base': {'ref': 'main', 'sha': 'base'}}
        self.api = Mock(repository=self.repo)
        self.updater = u.Updater(self.api, sleep=Mock())

    def test_only_terminal_digest_changes_and_mutable_tags_are_rejected(self):
        self.assertEqual({k for k in self.lock if self.lock[k] != self.updated[k]}, {'terminal'})
        self.assertEqual(self.lock, json.loads((ROOT/'images.lock.json').read_text()))
        for invalid in ['latest', 'sha256:'+'f'*63, 'sha256:'+'F'*64, self.digest+'\n']:
            with self.assertRaises(u.UpdateError): u.replace_terminal(self.lock, invalid)

    def test_registry_digest_and_linux_amd64_are_verified(self):
        manifest = {'schemaVersion': 2, 'manifests': [{'platform': {'architecture': 'amd64', 'os': 'linux'},
                                                    'digest': self.digest}]}
        raw = json.dumps(manifest).encode()
        digest = 'sha256:' + u.hashlib.sha256(raw).hexdigest()
        with patch.object(u, 'read_url', side_effect=[(b'{"token":"synthetic"}', {}),
                         (raw, {'Docker-Content-Digest': digest})]):
            self.assertEqual(u.latest_digest(), digest)
        with patch.object(u, 'read_url', side_effect=[(b'{"token":"synthetic"}', {}),
                         (raw, {'Docker-Content-Digest': self.digest})]):
            with self.assertRaisesRegex(u.UpdateError, 'registry-digest-mismatch'): u.latest_digest()
        manifest['manifests'][0]['platform']['architecture'] = 'arm64'
        raw = json.dumps(manifest).encode()
        digest = 'sha256:' + u.hashlib.sha256(raw).hexdigest()
        with patch.object(u, 'read_url', side_effect=[(b'{"token":"synthetic"}', {}),
                         (raw, {'Docker-Content-Digest': digest})]):
            with self.assertRaisesRegex(u.UpdateError, 'linux-amd64-image-required'): u.latest_digest()

    def test_success_requires_both_jobs_on_the_exact_dispatched_head(self):
        run = {'head_sha': 'head', 'event': 'workflow_dispatch', 'status': 'completed', 'conclusion': 'success'}
        jobs = [{'name': name, 'status': 'completed', 'conclusion': 'success'} for name in u.REQUIRED_JOBS]
        self.assertTrue(u.passed_jobs(run, jobs, 'head'))
        self.assertFalse(u.passed_jobs(run, jobs[:1], 'head'))
        self.assertFalse(u.passed_jobs({**run, 'head_sha': 'stale'}, jobs, 'head'))
        self.assertFalse(u.passed_jobs({**run, 'event': 'pull_request'}, jobs, 'head'))
        self.assertFalse(u.passed_jobs(run, [{**j, 'conclusion': 'failure'} for j in jobs], 'head'))

    def test_no_new_image_does_not_open_pr_or_dispatch_for_human_commit(self):
        self.updater.main_sha = Mock(return_value='base')
        self.api.lock.return_value = self.lock
        self.api.return_value = {'author': {'email': 'noreply@github.com'}, 'message': 'Other change'}
        self.updater.candidate = Mock()
        self.updater.production = Mock()
        result = self.updater.run(self.lock['terminal'].split('@')[1])
        self.assertEqual(result['status'], 'image-current')
        self.updater.candidate.assert_not_called(); self.updater.production.assert_not_called()

    def test_previous_bot_merge_recovers_a_missed_deployment_dispatch(self):
        self.updater.main_sha = Mock(return_value='base')
        self.api.lock.return_value = self.lock
        self.api.return_value = {'author': u.BOT, 'message': 'Update terminal image to ' + self.digest}
        self.updater.production = Mock(return_value=True)
        result = self.updater.run(self.lock['terminal'].split('@')[1])
        self.assertTrue(result['deploymentDispatched'])
        self.updater.production.assert_called_once_with('base')

    def test_failed_validation_leaves_pr_open_and_never_merges_or_deploys(self):
        self.updater.main_sha = Mock(return_value='base'); self.api.lock.return_value = self.lock
        self.updater.candidate = Mock(return_value=(self.branch, 'head', 7))
        self.updater.validate = Mock(side_effect=u.UpdateError('image-validation-failed-pr-left-open'))
        self.updater.merge = Mock(); self.updater.production = Mock()
        with self.assertRaises(u.UpdateError): self.updater.run(self.digest)
        self.updater.merge.assert_not_called(); self.updater.production.assert_not_called()

    def test_main_advance_blocks_merge(self):
        self.updater.main_sha = Mock(return_value='new-main')
        with self.assertRaisesRegex(u.UpdateError, 'main-advanced'):
            self.updater.merge('base', self.branch, 'head', 7, self.updated)
        self.api.assert_not_called()

    def test_merge_rechecks_owner_head_and_lockfile_only_scope(self):
        self.updater.main_sha = Mock(return_value='base')
        files = [{'filename': 'images.lock.json', 'status': 'modified'}]
        for change in ['valid', 'author', 'fork', 'head', 'other-file', 'auth-image']:
            with self.subTest(change=change):
                pr = copy.deepcopy(self.pr); changed_files = copy.deepcopy(files); lock = copy.deepcopy(self.updated)
                if change == 'author': pr['user']['login'] = 'human'
                if change == 'fork': pr['head']['repo']['full_name'] = 'other/infra'
                if change == 'head': pr['head']['sha'] = 'other-head'
                if change == 'other-file': changed_files.append({'filename': 'runtime/host.py', 'status': 'modified'})
                if change == 'auth-image': lock['auth'] = 'synthetic-change'
                self.api.reset_mock(); self.api.pages.return_value = changed_files; self.api.lock.return_value = lock
                self.api.side_effect = [pr, {'merged': True, 'sha': 'merged'}]
                if change == 'valid':
                    self.assertEqual(self.updater.merge('base', self.branch, 'head', 7, self.updated), 'merged')
                    self.assertEqual(self.api.call_args.args, ('/pulls/7/merge', 'PUT', {'sha': 'head', 'merge_method': 'rebase'}))
                else:
                    with self.assertRaises(u.UpdateError): self.updater.merge('base', self.branch, 'head', 7, self.updated)
                    self.assertFalse(any(len(call.args) > 1 and call.args[1] == 'PUT' for call in self.api.call_args_list))

    def test_dispatch_is_explicit_and_duplicate_successful_or_active_deployments_are_skipped(self):
        self.updater.main_sha = Mock(return_value='merged')
        for state, conclusion, dispatch in [('in_progress', None, False), ('completed', 'success', False),
                                            ('completed', 'failure', True)]:
            with self.subTest(state=state, conclusion=conclusion):
                self.api.reset_mock(); self.api.side_effect = None
                self.api.return_value = {'workflow_runs': [{'head_sha': 'merged', 'status': state, 'conclusion': conclusion}]}
                self.assertEqual(self.updater.production('merged'), dispatch)
                if dispatch:
                    self.assertEqual(self.api.call_args.args, ('/actions/workflows/production.yml/dispatches', 'POST',
                                                              {'ref': 'main', 'inputs': {'reconcile': True}}))
                else: self.assertEqual(self.api.call_count, 1)

    def test_image_updater_has_no_production_environment_or_identity(self):
        workflow = (ROOT/'.github/workflows/image-update.yml').read_text()
        self.assertNotIn('id-token:', workflow); self.assertNotIn('environment:', workflow)
        self.assertNotIn('secrets.', workflow); self.assertNotIn('pull_request_target', workflow)
        self.assertIn("github.ref == 'refs/heads/main'", workflow)
        self.assertIn('persist-credentials: false', workflow)
        self.assertIn('23 2 * * *', workflow)
        self.assertIn('workflow_dispatch:', (ROOT/'.github/workflows/ci.yml').read_text())

    def test_candidate_changes_only_lockfile_and_closes_only_stale_bot_pr(self):
        stale = copy.deepcopy(self.pr); stale['number'] = 6; stale['head']['ref'] = u.PREFIX + 'old'
        human = copy.deepcopy(stale); human['number'] = 5; human['user']['login'] = 'human'
        fork = copy.deepcopy(stale); fork['number'] = 4; fork['head']['repo'] = None
        self.api.optional.return_value = None
        self.api.pages.return_value = [stale, human, fork]
        self.api.side_effect = [
            {'tree': {'sha': 'base-tree'}}, {'sha': 'blob'}, {'sha': 'tree'},
            {'sha': 'head'}, None, self.pr, None]
        branch, head, number = self.updater.candidate('base', self.digest, self.updated)
        self.assertEqual((head, number), ('head', 7))
        calls = self.api.call_args_list
        self.assertEqual(calls[2].args, ('/git/trees', 'POST', {'base_tree': 'base-tree', 'tree': [
            {'path': 'images.lock.json', 'mode': '100644', 'type': 'blob', 'sha': 'blob'}]}))
        self.assertEqual(calls[3].args[2]['parents'], ['base'])
        self.assertEqual(calls[3].args[2]['author'], u.BOT)
        self.assertEqual(calls[-1].args, ('/pulls/6', 'PATCH', {'state': 'closed'}))
        self.assertFalse(any(call.args[0] in ['/pulls/5', '/pulls/4'] for call in calls))

    def test_candidate_reuses_an_unchanged_bot_pr_without_a_new_commit(self):
        self.pr['head']['ref'] = u.PREFIX + 'f'*16 + '-base'
        self.api.optional.return_value = {'object': {'sha': 'head'}}
        self.api.pages.return_value = [self.pr]
        self.api.side_effect = [
            {'tree': {'sha': 'base-tree'}}, {'sha': 'blob'}, {'sha': 'tree'},
            {'tree': {'sha': 'tree'}, 'parents': [{'sha': 'base'}], 'author': u.BOT}]
        self.assertEqual(self.updater.candidate('base', self.digest, self.updated), (self.pr['head']['ref'], 'head', 7))
        self.assertFalse(any(call.args[0] in ['/git/commits', '/git/refs', '/pulls'] for call in self.api.call_args_list))

    def test_candidate_never_overwrites_a_modified_automation_branch(self):
        self.api.optional.return_value = {'object': {'sha': 'human-head'}}
        self.api.side_effect = [
            {'tree': {'sha': 'base-tree'}}, {'sha': 'blob'}, {'sha': 'tree'},
            {'tree': {'sha': 'other-tree'}, 'parents': [{'sha': 'base'}], 'author': u.BOT}]
        with self.assertRaisesRegex(u.UpdateError, 'automation-branch-modified'):
            self.updater.candidate('base', self.digest, self.updated)
        self.api.pages.assert_not_called()
        self.assertFalse(any(len(call.args) > 1 and call.args[1] == 'PATCH' for call in self.api.call_args_list))

    def test_validation_dispatches_ci_and_waits_for_its_exact_head(self):
        run = {'id': 10, 'head_sha': 'head', 'head_branch': self.branch, 'event': 'workflow_dispatch',
               'status': 'completed', 'conclusion': 'success'}
        jobs = [{'name': name, 'status': 'completed', 'conclusion': 'success'} for name in u.REQUIRED_JOBS]
        wrong = {**run, 'head_sha': 'stale'}
        self.updater.validation_status = Mock()
        self.api.pages.return_value = jobs
        self.api.side_effect = [{'workflow_runs': [wrong]}, None, {'workflow_runs': [run]}]
        self.updater.validate(self.branch, 'head')
        self.assertEqual(self.api.call_args_list[1].args, ('/actions/workflows/ci.yml/dispatches', 'POST', {'ref': self.branch}))
        self.api.pages.assert_called_once_with('/actions/runs/10/jobs')
        self.assertEqual(self.updater.validation_status.call_args_list[0].args, ('head', 'pending'))
        self.updater.validation_status.assert_called_with('head', 'success', 10)

    def test_existing_failed_checks_are_not_silently_bypassed_or_repeated(self):
        run = {'id': 10, 'head_sha': 'head', 'head_branch': self.branch, 'event': 'workflow_dispatch',
               'status': 'completed', 'conclusion': 'failure'}
        self.updater.validation_status = Mock()
        self.api.pages.return_value = []
        self.api.side_effect = [{'workflow_runs': [run]}, {'workflow_runs': [run]}]
        with self.assertRaisesRegex(u.UpdateError, 'image-validation-failed-pr-left-open'):
            self.updater.validate(self.branch, 'head')
        self.assertTrue(all(len(call.args) == 1 for call in self.api.call_args_list))
        self.updater.validation_status.assert_called_with('head', 'failure', 10)

    def test_successful_update_merges_then_explicitly_dispatches_production(self):
        self.updater.main_sha = Mock(return_value='base'); self.api.lock.return_value = self.lock
        self.updater.candidate = Mock(return_value=(self.branch, 'head', 7))
        self.updater.validate = Mock(); self.updater.merge = Mock(return_value='merged')
        self.updater.production = Mock(return_value=True)
        result = self.updater.run(self.digest)
        self.assertEqual(result, {'status': 'image-update-merged', 'pullRequest': 7, 'deploymentDispatched': True})
        self.updater.validate.assert_called_once_with(self.branch, 'head')
        self.updater.merge.assert_called_once_with('base', self.branch, 'head', 7, self.updated)
        self.updater.production.assert_called_once_with('merged')

    def validation_api(self, *, conclusion='success', jobs=None):
        run = {'id': 10, 'head_sha': 'head', 'head_branch': self.branch, 'event': 'workflow_dispatch',
               'status': 'completed', 'conclusion': conclusion}
        self.api.pages.return_value = jobs if jobs is not None else [
            {'name': name, 'status': 'completed', 'conclusion': 'success'} for name in u.REQUIRED_JOBS]
        def call(path, method='GET', body=None):
            if path.startswith('/statuses/'):
                self.assertEqual(method, 'POST')
                self.assertEqual(path, '/statuses/head')
                return {}
            self.assertEqual(method, 'GET')
            return {'workflow_runs': [run]}
        self.api.side_effect = call

    def statuses(self):
        return [call.args[2] for call in self.api.call_args_list if call.args[0] == '/statuses/head']

    def test_trusted_updater_publishes_both_exact_head_statuses_after_verified_jobs(self):
        self.validation_api()
        self.updater.validate(self.branch, 'head')
        statuses = self.statuses()
        self.assertEqual([(s['context'], s['state']) for s in statuses], [
            ('container-policies', 'pending'), ('validate', 'pending'),
            ('container-policies', 'success'), ('validate', 'success')])
        self.assertTrue(all(s['target_url'] == 'https://github.com/example/infra/actions/runs/10'
                            for s in statuses if s['state'] == 'success'))
        self.api.pages.assert_called_once_with('/actions/runs/10/jobs')

    def test_failed_skipped_or_missing_required_jobs_never_publish_success(self):
        for conclusion in ['failure', 'cancelled', 'skipped', 'success']:
            for jobs in [[], [{'name': 'validate', 'status': 'completed', 'conclusion': 'success'}],
                         [{'name': n, 'status': 'completed', 'conclusion': 'skipped'} for n in u.REQUIRED_JOBS]]:
                with self.subTest(conclusion=conclusion, jobs=jobs):
                    self.api.reset_mock()
                    self.validation_api(conclusion=conclusion, jobs=jobs)
                    with self.assertRaisesRegex(u.UpdateError, 'image-validation-failed-pr-left-open'):
                        self.updater.validate(self.branch, 'head')
                    self.assertEqual([s['state'] for s in self.statuses()], ['pending', 'pending', 'failure', 'failure'])

    def test_validation_timeout_reports_error_for_both_contexts(self):
        self.updater.now = Mock(side_effect=[0, 1801])
        self.api.return_value = {'workflow_runs': []}
        with self.assertRaisesRegex(u.UpdateError, 'image-validation-timeout'):
            self.updater.validate(self.branch, 'head')
        self.assertEqual([s['state'] for s in self.statuses()], ['pending', 'pending', 'error', 'error'])
        self.assertFalse(any(c.args[0].endswith('/merge') for c in self.api.call_args_list))

    def test_validation_api_error_never_leaves_success_statuses(self):
        def call(path, method='GET', body=None):
            if path.startswith('/statuses/'): return {}
            raise u.UpdateError('github-api-503')
        self.api.side_effect = call
        with self.assertRaisesRegex(u.UpdateError, 'github-api-503'):
            self.updater.validate(self.branch, 'head')
        self.assertEqual([s['state'] for s in self.statuses()], ['pending', 'pending', 'error', 'error'])

    def test_commit_status_permission_is_limited_to_trusted_updater(self):
        workflow = (ROOT/'.github/workflows/image-update.yml').read_text()
        self.assertIn('      statuses: write', workflow)
        validation = (ROOT/'.github/workflows/ci.yml').read_text()
        self.assertIn('  contents: read', validation)
        self.assertNotIn('statuses: write', validation)

    def test_merge_rejection_is_identified_without_exposing_github_response(self):
        import io
        error = u.urllib.error.HTTPError('https://api.github.com/', 405, 'blocked', {},
                                        io.BytesIO(b'{"message":"private details"}'))
        with patch.object(u, 'read_url', side_effect=error):
            with self.assertRaisesRegex(u.UpdateError, '^protected-merge-blocked-pr-left-open$'):
                u.GitHub(self.repo, 'synthetic')('/pulls/7/merge', 'PUT', {'sha': 'head'})
