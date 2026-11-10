"""Laptop server client: trusted host pins, isolated grants and secret transport."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import server_integration as servers


class ServerClient(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='Latch POC ')
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        (self.state / 'vm').mkdir()
        self.instance = '11111111-1111-1111-1111-111111111111'
        self.grant = {'grant_id': self.instance, 'state': 'active',
                      'gateway_host': '127.0.0.1', 'gateway_port': 19202,
                      'gateway_host_public_key': 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
                      'ssh_username': 'latch-server-test:latch-server-test'}

    def test_legacy_api_migration_stops_only_matching_service_without_removing_volume(self):
        with patch.object(servers.subprocess, 'run', return_value=Mock(stdout='abcdef123456\n')) as run:
            servers.retire_legacy_api({})
        listing = run.call_args_list[0].args[0]
        self.assertIn('label=com.docker.compose.project=latch-poc', listing)
        self.assertIn('label=com.docker.compose.service=vm-api', listing)
        self.assertEqual(run.call_args_list[1].args[0][-3:], ['docker', 'stop', 'abcdef123456'])
        self.assertTrue(all('rm' not in call.args[0] for call in run.call_args_list))

    def test_ssh_uses_api_endpoint_and_exact_pin_with_workspace_spaces(self):
        command = servers.ssh_command(self.state, {}, self.grant, 'show diagnostics', tty=False)
        self.assertEqual(command[-2:], ['latch-server-test:latch-server-test@127.0.0.1', 'show diagnostics'])
        self.assertNotIn('StrictHostKeyChecking=accept-new', command)
        self.assertIn('StrictHostKeyChecking=yes', command)
        parsed = subprocess.run(['ssh', '-G', *command[1:]], capture_output=True, text=True)
        self.assertEqual(parsed.returncode, 0, parsed.stderr)
        self.assertIn('port 19202', parsed.stdout)
        self.assertIn('userknownhostsfile ' + str(self.state / 'warpgate_known_hosts'), parsed.stdout)
        pin = self.state / 'warpgate_known_hosts'
        self.assertEqual(pin.read_text(), 'latch-warpgate ' + self.grant['gateway_host_public_key'] + '\n')
        self.assertEqual(pin.stat().st_mode & 0o777, 0o600)

    def test_changed_pin_never_overwrites_trusted_identity(self):
        pin = self.state / 'warpgate_known_hosts'
        pin.write_text('latch-warpgate ssh-ed25519 DIFFERENT\n')
        with self.assertRaisesRegex(RuntimeError, 'conflicts'):
            servers.ssh_command(self.state, {}, self.grant)
        self.assertEqual(pin.read_text(), 'latch-warpgate ssh-ed25519 DIFFERENT\n')

    def test_inactive_grant_or_malformed_pin_rejected_before_writing(self):
        for changes in ({'state': 'revoked'}, {'gateway_host_public_key': 'ssh-rsa AAAA'},
                        {'gateway_host_public_key': 'ssh-ed25519 AAAA\nextra'}):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                servers.ssh_command(self.state, {}, {**self.grant, **changes})
            self.assertFalse((self.state / 'warpgate_known_hosts').exists())

    def test_issue_selects_single_inventory_and_transmits_only_public_key(self):
        (self.state / 'vm/inventory.json').write_text(json.dumps({'vms': {self.instance: {}}}))
        (self.state / 'gateway_ed25519.pub').write_text('ssh-ed25519 laptop-public\n')
        (self.state / 'gateway_ed25519').write_text('NEVER SEND PRIVATE KEY')
        with patch.object(servers, 'request', return_value=self.grant) as request:
            self.assertEqual(servers.issue(self.state, {}, ttl_seconds=60), self.grant)
        self.assertEqual(request.call_args.args, ({}, 'POST', '/v1/server-grants',
                         {'server_id': self.instance, 'ttl_seconds': 60,
                          'public_key': 'ssh-ed25519 laptop-public'}))
        self.assertTrue(request.call_args.kwargs['idempotency_key'])
        self.assertNotIn('PRIVATE', str(request.call_args))

    def test_ambiguous_inventory_requires_explicit_instance(self):
        (self.state / 'vm/inventory.json').write_text(json.dumps({'vms': {'a': {}, 'b': {}}}))
        with patch.object(servers, 'request') as request, self.assertRaisesRegex(RuntimeError, '--server'):
            servers.issue(self.state, {})
        request.assert_not_called()

    def test_cached_grants_are_revalidated_and_only_active_returned(self):
        self.assertIsNone(servers.current(self.state, {}))
        servers.remember(self.state, self.grant)
        cache = self.state / 'vm/current-grant.json'
        self.assertEqual(json.loads(cache.read_text()), {'grant_id': self.instance})
        self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
        with patch.object(servers, 'request', return_value=self.grant) as request:
            self.assertEqual(servers.current(self.state, {}), self.grant)
            request.assert_called_once_with({}, 'GET', '/v1/server-grants/' + self.instance)
        with patch.object(servers, 'request', return_value={**self.grant, 'state': 'revoked'}):
            self.assertIsNone(servers.current(self.state, {}))

    def test_request_payload_uses_stdin_and_reads_token_only_inside_container(self):
        payload = {'public_key': 'laptop-public-key'}
        with patch.object(servers.subprocess, 'run', return_value=Mock(stdout='{"state":"active"}')) as run:
            self.assertEqual(servers.request({}, 'POST', '/v1/server-grants', payload,
                                        idempotency_key='retry-1'), {'state': 'active'})
        argv = run.call_args.args[0]
        self.assertNotIn('laptop-public-key', str(argv))
        self.assertIn('/run/server-api/api-token', argv[-1])
        self.assertIn('10001:10001', argv)
        self.assertEqual(json.loads(run.call_args.kwargs['input']),
                         {'method': 'POST', 'path': '/v1/server-grants', 'payload': payload,
                          'idempotency_key': 'retry-1'})

    def test_request_failure_reports_status_without_upstream_details(self):
        error = subprocess.CalledProcessError(1, ['secret-bearing-upstream-command'],
                                              stderr='private upstream detail\nServer API HTTP 503\n')
        with patch.object(servers.subprocess, 'run', side_effect=error):
            with self.assertRaisesRegex(RuntimeError, '^Server API request failed: HTTP 503$'):
                servers.request({}, 'POST', '/v1/server-grants', {})


if __name__ == '__main__':
    unittest.main()
