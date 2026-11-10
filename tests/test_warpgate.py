import base64
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
import warpgate


class FakeApi:
    def __init__(self):
        self.objects = {'/roles': [], '/users': [], '/secret-backends': [],
                        '/targets': [], '/ssh/known-hosts': []}
        self.calls = []

    def request(self, method, path, data=None, **kwargs):
        self.calls.append((method, path, data))
        if path.endswith('/health'):
            return {'error': None}
        if path == '/parameters':
            return {}
        if path.endswith('/roles') and '/targets/' in path:
            return [{'id': '1'}]  # first created role
        if '/roles/' in path:
            return None
        if '/credentials/public-keys' in path:
            self.objects.setdefault(path, [])
        if method == 'GET':
            return self.objects[path]
        if method == 'POST':
            result = dict(data, id=str(sum(len(v) for v in self.objects.values()) + 1))
            self.objects[path].append(result)
            return result
        if method == 'PUT':
            parent, identifier = path.rsplit('/', 1)
            result = dict(data, id=identifier)
            self.objects[parent] = [result if v['id'] == identifier else v for v in self.objects[parent]]
            return result
        raise AssertionError((method, path))


class WarpgateBootstrap(unittest.TestCase):
    def test_reconciliation_preserves_user_key_and_scopes_signer(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / 'gateway_ed25519.pub').write_text('ssh-ed25519 ZmFrZQ== laptop')
            api = FakeApi()
            args = dict(role_id='dedicated-role', secret_id='dedicated-secret',
                        host='100.64.10.120', port=22001,
                        host_public_key='ssh-ed25519 dGFyZ2V0')
            first = warpgate.bootstrap(api, state, **args)
            second = warpgate.bootstrap(api, state, **args)
            self.assertEqual(first, second)
            for path in ['/users', '/targets', '/secret-backends', '/ssh/known-hosts']:
                self.assertEqual(len(api.objects[path]), 1)
            backend = api.objects['/secret-backends'][0]
            self.assertEqual(backend['allowed_paths'], ['ssh-client-signer/sign/latch-warpgate'])
            self.assertEqual(backend['auth']['method'], 'AppRole')
            target = api.objects['/targets'][0]['options']
            self.assertEqual(target['auth']['kind'], 'Certificate')
            self.assertEqual(target['username'], 'ubuntu')
            self.assertNotIn('password', target['auth'])

    def test_changed_target_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / 'gateway_ed25519.pub').write_text('ssh-ed25519 ZmFrZQ==')
            api = FakeApi()
            api.objects['/ssh/known-hosts'] = [{'host': 'vm', 'port': 22,
                'key_type': 'ssh-ed25519', 'key_base64': 'b2xk'}]
            with self.assertRaisesRegex(RuntimeError, 'host key changed'):
                warpgate.bootstrap(api, state, role_id='r', secret_id='s', host='vm',
                                   host_public_key='ssh-ed25519 bmV3')
            self.assertFalse(any(m == 'POST' and p == '/ssh/known-hosts' for m, p, _ in api.calls))

    def test_prepare_is_idempotent_and_private(self):
        with tempfile.TemporaryDirectory(prefix='Latch POC ') as directory:
            state = Path(directory)
            warpgate.prepare(state)
            password = (state / 'warpgate/admin-password').read_bytes()
            key = (state / 'gateway_ed25519').read_bytes()
            warpgate.prepare(state)
            self.assertEqual(password, (state / 'warpgate/admin-password').read_bytes())
            self.assertEqual(key, (state / 'gateway_ed25519').read_bytes())
            self.assertEqual((state / 'warpgate/admin-password').stat().st_mode & 0o777, 0o600)

    def test_ssh_paths_with_spaces_are_parseable(self):
        command = warpgate.ssh_command('/tmp/Latch POC', {'LAB_WORKER_PORT': '19202'})
        parsed = subprocess.run(['ssh', '-G', *command[1:]], capture_output=True, text=True)
        self.assertEqual(parsed.returncode, 0, parsed.stderr)
        self.assertIn('userknownhostsfile /tmp/Latch POC/warpgate_known_hosts', parsed.stdout)
        self.assertIn('user latch:latch-vm', parsed.stdout)
        self.assertIn('port 19202', parsed.stdout)


class WarpgateRecording(unittest.TestCase):
    def test_decodes_fragmented_output_and_ignores_input(self):
        api = Mock()
        def request(method, path, **kwargs):
            if path.startswith('/sessions?'):
                return {'items': [{'id': 's', 'target_sessions': [{'target': {'name': 'latch-vm'}}]}]}
            if path.endswith('/recordings'):
                return [{'id': 'r'}]
            return b'\n'.join(json.dumps({'time': 0, 'stream': 'Output',
                'data': base64.b64encode(piece).decode()}).encode() for piece in [b'verify-', b'123'])
        api.request.side_effect = request
        self.assertEqual(warpgate.verify_recording(api, 'verify-123'),
                         {'session_id': 's', 'recording_id': 'r'})

    def test_input_is_not_accepted_as_execution_evidence(self):
        api = Mock()
        api.request.side_effect = [
            {'items': [{'id': 's', 'target_sessions': [{'target': {'name': 'latch-vm'}}]}]},
            [{'id': 'r'}], json.dumps({'stream': 'Input', 'data': base64.b64encode(b'marker').decode()}).encode()]
        with self.assertRaisesRegex(RuntimeError, 'No Warpgate SSH recording'):
            warpgate.verify_recording(api, 'marker')

    def test_close_only_latch_ssh_sessions(self):
        api = Mock()
        api.request.side_effect = [{'items': [{'id': 'ssh', 'protocol': 'SSH'},
                                            {'id': 'web', 'protocol': 'HTTP'}]}, None]
        self.assertEqual(warpgate.close_sessions(api), 1)
        api.request.assert_any_call('POST', '/sessions/ssh/close')

    def test_http_error_body_is_not_exposed(self):
        api = object.__new__(warpgate.Api)
        api.address = 'https://localhost'
        api.opener = Mock()
        api.opener.open.side_effect = urllib.error.HTTPError('https://localhost', 403,
            'secret', {}, io.BytesIO(b'credential-secret'))
        with self.assertRaisesRegex(RuntimeError, 'HTTP 403') as caught:
            api.request('POST', '/secret-backends', {'secret': 'credential-secret'})
        self.assertNotIn('credential-secret', str(caught.exception))

    def test_long_running_client_renews_expired_admin_cookie_once(self):
        with tempfile.TemporaryDirectory() as directory:
            api = object.__new__(warpgate.Api)
            api.address = 'https://localhost'
            api.password_file = Path(directory) / 'password'
            api.password_file.write_text('private-admin-password')
            api.opener = Mock()
            rejected = urllib.error.HTTPError('https://localhost', 401, 'expired', {}, io.BytesIO())
            login, success = Mock(), Mock()
            login.__enter__ = Mock(return_value=Mock(read=Mock(return_value=b'{}')))
            login.__exit__ = Mock(return_value=False)
            success.__enter__ = Mock(return_value=Mock(read=Mock(return_value=b'[]')))
            success.__exit__ = Mock(return_value=False)
            api.opener.open.side_effect = [rejected, login, success]
            self.assertEqual(api.request('GET', '/targets'), [])
            self.assertEqual(api.opener.open.call_count, 3)
            login_request = api.opener.open.call_args_list[1].args[0]
            self.assertTrue(login_request.full_url.endswith('/api/auth/login'))
            api.opener.open.side_effect = [rejected, login, rejected]
            with self.assertRaisesRegex(RuntimeError, 'HTTP 401'):
                api.request('GET', '/targets')

    def test_internal_service_requests_select_control_plane_host(self):
        api = object.__new__(warpgate.Api)
        api.address = 'https://warpgate:8888'
        api.control_plane_host = 'localhost'
        api.opener = Mock()
        response = Mock()
        response.__enter__ = Mock(return_value=Mock(read=Mock(return_value=b'{}')))
        response.__exit__ = Mock(return_value=False)
        api.opener.open.return_value = response
        for method, path, admin in [('POST', '/auth/login', False), ('GET', '/parameters', True)]:
            api.request(method, path, {}, admin=admin)
            request = api.opener.open.call_args.args[0]
            self.assertEqual(request.host, 'warpgate:8888')
            self.assertEqual(request.get_header('Host'), 'localhost')


if __name__ == '__main__':
    unittest.main()
