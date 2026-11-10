import base64
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'runtime'), str(ROOT / 'operations'), str(ROOT / 'tools')]
import host
from render import compose, proxy_config, caddy_config
from controller import validate


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.images = json.loads((ROOT / 'images.lock.json').read_text())
        self.ownership = patch.object(host.os, 'fchown').start()
        self.addCleanup(patch.stopall)

    def test_terminal_has_no_credentials_and_only_persistent_data_mounts(self):
        for size in ['Standard_B1ms', 'Standard_B2als_v2']:
            terminal = compose(self.images, size)['services']['terminal']
            self.assertEqual(terminal['user'], '1000:1000')
            self.assertEqual(terminal['cap_drop'], ['ALL'])
            self.assertEqual(terminal['ports'], ['127.0.0.1:7681:7681'])
            self.assertEqual(set(terminal['volumes']), {'/srv/codex-terminal/workspace:/workspace',
                '/srv/codex-terminal/codex-config:/home/node/.codex'})
            self.assertNotIn('environment', terminal)
            self.assertNotIn('network_mode', terminal)
            self.assertIn('no-new-privileges:true', terminal['security_opt'])
            self.assertNotIn('privileged', terminal)

    def test_proxy_uses_immutable_owner_and_never_forwards_tokens(self):
        value = tomllib.loads(proxy_config('tenant', 'client', 'owner', 'terminal.example.com', 'synthetic', 'synthetic'))
        self.assertEqual(value['allowed_groups'], ['owner'])
        self.assertEqual(value['oidc_groups_claim'], 'oid')
        self.assertEqual(value['code_challenge_method'], 'S256')
        self.assertEqual(value['redirect_url'], 'https://terminal.example.com/oauth2/callback')
        for field in ['insecure_oidc_skip_nonce', 'pass_access_token', 'pass_authorization_header',
                      'set_authorization_header', 'pass_basic_auth', 'pass_user_headers']:
            self.assertFalse(value[field])
        self.assertTrue(value['cookie_secure'])
        self.assertTrue(value['cookie_httponly'])

    def test_seccomp_is_default_deny_and_allows_inner_sandbox(self):
        profile = json.loads((ROOT / 'runtime/seccomp.json').read_text())
        self.assertEqual(profile['defaultAction'], 'SCMP_ACT_ERRNO')
        allowed = {n for rule in profile['syscalls'] if rule['action'] == 'SCMP_ACT_ALLOW' and not rule.get('includes') for n in rule['names']}
        self.assertTrue({'unshare','mount','umount2','pivot_root'} <= allowed)
        self.assertTrue({'init_module','finit_module','kexec_load'}.isdisjoint(allowed))

    def files(self):
        files = {name: b'synthetic\n' for name in host.FILES}
        files['compose.json'] = json.dumps(compose(self.images, 'Standard_B1ms')).encode()
        files['docker-daemon.json'] = (ROOT / 'runtime/docker-daemon.json').read_bytes()
        files['seccomp.json'] = (ROOT / 'runtime/seccomp.json').read_bytes()
        files['oauth2-proxy.cfg'] = proxy_config('tenant','client','owner','terminal.example.com','synthetic','synthetic').encode()
        return files

    def install(self, root, files):
        self.addCleanup(patch.stopall)
        patch.object(host, 'OS_FILES', {}).start()
        for name, data in files.items(): (root / name).write_bytes(data)

    def payload(self):
        return {'commit':'test', 'fingerprint':'test', 'images':self.images}

    def test_unchanged_apply_does_not_write_recreate_or_rotate(self):
        files = self.files()
        with tempfile.TemporaryDirectory() as directory, patch.object(host, 'ROOT', Path(directory)), \
             patch.object(host, 'RECORDS', Path(directory)/'revisions'), patch.object(host,'verify'), \
             patch.object(host,'write') as write, patch.object(host,'run') as run, patch.object(host,'compose') as compose_run:
            self.install(Path(directory),files)
            before = host.live_hashes()
            payload = {**self.payload(),'expectedBefore':before}
            result = host.apply(payload,files)
            self.assertEqual(result['status'],'unchanged')
            self.assertEqual(before,host.live_hashes())
            write.assert_not_called();run.assert_not_called();compose_run.assert_not_called()

    def test_image_update_affects_only_terminal(self):
        files = self.files()
        with tempfile.TemporaryDirectory() as directory, patch.object(host,'ROOT',Path(directory)):
            self.install(Path(directory),files)
            updated = json.loads(files['compose.json'])
            updated['services']['terminal']['image'] = 'synthetic@sha256:' + 'f'*64
            files['compose.json'] = json.dumps(updated).encode()
            result = host.changes(files)
            self.assertEqual(result['changed'],['compose.json'])
            self.assertEqual(result['affected'],['terminal'])

    def test_failed_image_restores_configuration_without_restoring_data(self):
        files = self.files()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);self.install(root,files)
            workspace = root/'workspace';workspace.mkdir();(workspace/'saved').write_text('newest contents')
            original = (root/'compose.json').read_bytes()
            updated = json.loads(files['compose.json']);updated['services']['terminal']['image']='deliberate-failure'
            files['compose.json']=json.dumps(updated).encode()
            with patch.object(host,'ROOT',root),patch.object(host,'RECORDS',root/'revisions'), \
                 patch.object(host,'run'),patch.object(host,'compose') as commands, \
                 patch.object(host,'inspect',return_value={s:{'Id':s} for s in host.SERVICES}), \
                 patch.object(host,'verify',side_effect=RuntimeError('synthetic-failure')), \
                 patch.object(host.time,'monotonic',side_effect=[0,500]):
                with self.assertRaises(RuntimeError):
                    host.apply({**self.payload(),'expectedBefore':host.live_hashes()},files)
                self.assertEqual((root/'compose.json').read_bytes(),original)
                self.assertEqual((workspace/'saved').read_text(),'newest contents')
                self.assertTrue(any(c.args[-1]=='terminal' for c in commands.call_args_list))
                self.assertFalse(any('auth' in c.args or 'https' in c.args for c in commands.call_args_list))
                self.assertFalse((root/'pending.json').exists())

    def test_concurrent_change_is_rejected_before_mutation(self):
        files = self.files()
        with tempfile.TemporaryDirectory() as directory,patch.object(host,'ROOT',Path(directory)),patch.object(host,'write') as write:
            self.install(Path(directory),files)
            with self.assertRaises(AssertionError):host.apply({**self.payload(),'expectedBefore':{}},files)
            write.assert_not_called()

    def test_semantic_comparison_preserves_equivalent_existing_proxy(self):
        original = proxy_config('tenant','client','owner','terminal.example.com','synthetic','synthetic').encode()
        previous = original.replace(b'pass_host_header = true\n',b'')
        self.assertEqual(host.digest('oauth2-proxy.cfg',original),host.digest('oauth2-proxy.cfg',previous))

    def test_schemas_reject_unknown_private_fields_and_mutable_image_tags(self):
        validate(self.images,'images')
        invalid = {**self.images,'terminal':'ghcr.io/dorseg/codex-terminal:latest'}
        with self.assertRaises(Exception):validate(invalid,'images')
        deployment=json.loads((ROOT/'config/deployment.json').read_text())
        with self.assertRaises(Exception):validate({**deployment,'contactEmail':'owner@example.com'},'deployment')
