"""Regressions for the laptop SSH command, including paths containing spaces."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import lab


class ConnectCommand(unittest.TestCase):
    def test_ssh_options_accept_workspace_spaces(self):
        state = Path('/tmp/Latch POC')
        with patch.object(lab, 'STATE', state), \
             patch.object(lab, 'backend', return_value='legacy'), \
             patch.object(lab, 'settings', return_value={'LAB_WORKER_PORT': '19202'}), \
             patch.object(lab, 'run') as run:
            lab.connect()
        command = run.call_args.args[0]
        parsed = subprocess.run(['ssh', '-G', *command[1:]],
                                capture_output=True, text=True)
        self.assertEqual(parsed.returncode, 0, parsed.stderr)
        self.assertIn('user latch:latch-vm', parsed.stdout)
        self.assertIn('port 19202', parsed.stdout)
        self.assertIn('userknownhostsfile /tmp/Latch POC/warpgate_known_hosts', parsed.stdout)
        self.assertIn('batchmode yes', parsed.stdout)
        self.assertIn('-tt', command)
        self.assertNotIn('CertificateFile=', ' '.join(command))

    def test_verification_requires_vm_observed_gateway_source(self):
        for source, accepted in [('100.64.10.10 40000 192.168.20.15 22', True),
                                 ('100.64.10.2 40000 192.168.20.15 22', False)]:
            with self.subTest(source=source), \
                 patch.object(lab, 'backend', return_value='legacy'), \
                 patch.object(lab, 'settings', return_value={'LAB_WORKER_PORT': '9202'}), \
                 patch.object(lab, 'run', return_value=Mock(stdout='latch-vm\n' + source)) as run, \
                 patch('builtins.print'):
                if accepted:
                    self.assertIn(source, lab.connect(interactive=False))
                else:
                    with self.assertRaisesRegex(RuntimeError, 'gateway source IP'):
                        lab.connect(interactive=False)
                command = run.call_args.args[0]
                self.assertIn('-T', command)
                self.assertEqual(run.call_args.kwargs['timeout'], 90)

    def test_recording_marker_command_passes_through_ssh(self):
        with patch.object(lab, 'settings', return_value={'LAB_WORKER_PORT': '9202'}), \
             patch.object(lab, 'backend', return_value='legacy'), \
             patch.object(lab, 'run', return_value=Mock(stdout='100.64.10.10 40000 192.168.20.15 22')) as run, \
             patch('builtins.print'):
            lab.connect(interactive=False, remote_command='echo recording-marker; echo "$SSH_CONNECTION"')
        self.assertEqual(run.call_args.args[0][-1], 'echo recording-marker; echo "$SSH_CONNECTION"')


class ServerConnectLifecycle(unittest.TestCase):
    def setUp(self):
        self.grant = {'grant_id': '11111111-1111-1111-1111-111111111111', 'state': 'active'}
        self.stack = __import__('contextlib').ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(lab, 'backend', return_value='lima'))
        self.stack.enter_context(patch.object(lab, 'settings', return_value={}))
        self.issue = self.stack.enter_context(patch.object(lab.server_integration, 'issue', return_value=self.grant))
        self.current = self.stack.enter_context(patch.object(lab.server_integration, 'current'))
        self.request = self.stack.enter_context(patch.object(lab.server_integration, 'request', return_value=self.grant))
        self.ssh = self.stack.enter_context(patch.object(lab.server_integration, 'ssh_command', return_value=['ssh', 'target']))
        self.revoke = self.stack.enter_context(patch.object(lab.server_integration, 'revoke', return_value={'state': 'revoked'}))
        self.run = self.stack.enter_context(patch.object(lab, 'run', return_value=Mock(stdout='100.64.10.10 40000 192.168.20.15 22')))
        self.stack.enter_context(patch('builtins.print'))

    def test_connect_requires_explicit_grant_without_creating_or_reusing_one(self):
        with self.assertRaisesRegex(RuntimeError, 'requires --grant'):
            lab.connect()
        self.issue.assert_not_called()
        self.current.assert_not_called()
        self.request.assert_not_called()
        self.run.assert_not_called()
        self.revoke.assert_not_called()

    def test_explicit_grant_is_preserved_after_success(self):
        lab.connect(grant_id=self.grant['grant_id'])
        self.issue.assert_not_called()
        self.current.assert_not_called()
        self.revoke.assert_not_called()

    def test_explicit_grant_is_preserved_if_pin_validation_fails(self):
        self.ssh.side_effect = RuntimeError('Gateway pin conflicts')
        with self.assertRaisesRegex(RuntimeError, 'pin conflicts'):
            lab.connect(grant_id=self.grant['grant_id'])
        self.run.assert_not_called()
        self.revoke.assert_not_called()

    def test_explicit_grant_is_preserved_even_when_ssh_fails(self):
        self.run.side_effect = subprocess.CalledProcessError(255, ['ssh'])
        with self.assertRaises(subprocess.CalledProcessError):
            lab.connect(grant_id=self.grant['grant_id'])
        self.request.assert_called_once_with({}, 'GET', '/v1/server-grants/' + self.grant['grant_id'])
        self.issue.assert_not_called()
        self.revoke.assert_not_called()

    def test_cli_rejects_connect_without_grant(self):
        for command in ('server-connect', 'connect'):
            with self.subTest(command=command), patch.object(sys, 'argv', ['lab', command]), \
                 patch('sys.stderr'), self.assertRaises(SystemExit) as error:
                lab.main()
            self.assertEqual(error.exception.code, 2)
        self.issue.assert_not_called()
        self.request.assert_not_called()

    def test_verify_passes_one_explicit_grant_and_cleans_it_on_failure(self):
        self.current.return_value = None
        with patch.object(sys, 'argv', ['lab', 'verify']), \
             patch.object(lab, 'sync'), patch.object(lab, 'guest'), \
             patch.object(lab, 'verify_worker'), \
             patch.object(lab.server_integration, 'remember'), \
             patch.object(lab, 'verify_certificates', side_effect=RuntimeError('acceptance failed')) as verify:
            with self.assertRaisesRegex(RuntimeError, 'acceptance failed'):
                lab.main()
        verify.assert_called_once_with(grant_id=self.grant['grant_id'])
        self.revoke.assert_called_once_with(lab.STATE, {}, self.grant['grant_id'])

    def test_verify_preserves_existing_grant(self):
        self.current.return_value = self.grant
        with patch.object(sys, 'argv', ['lab', 'verify']), \
             patch.object(lab, 'sync'), patch.object(lab, 'guest'), \
             patch.object(lab, 'verify_worker'), \
             patch.object(lab, 'verify_certificates') as verify:
            lab.main()
        verify.assert_called_once_with(grant_id=self.grant['grant_id'])
        self.issue.assert_not_called()
        self.revoke.assert_not_called()


class SupportedCommands(unittest.TestCase):
    def test_admin_grant_passes_explicit_role_to_api(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / 'gateway_ed25519.pub').write_text('ssh-ed25519 public-key\n')
            with patch.object(sys, 'argv', ['lab', 'device-grant', '--device', 'freertr', '--role', 'admin']), \
                 patch.object(lab, 'STATE', state), \
                 patch.object(lab, 'backend', return_value='lima'), \
                 patch.object(lab, 'settings', return_value={}), \
                 patch.object(lab.device_integration, 'request', return_value={}) as request, \
                 patch('builtins.print'):
                lab.main()
            self.assertEqual(request.call_args.args, ({}, 'POST', '/v1/device-grants',
                {'device_id': 'freertr', 'role': 'admin', 'ttl_seconds': 300,
                 'public_key': 'ssh-ed25519 public-key'}))

    def test_retired_appliance_commands_are_rejected(self):
        for command in ('ise-up', 'ise-verify', 'ise-plan'):
            with self.subTest(command=command), \
                 patch.object(sys, 'argv', ['lab', command]), \
                 patch('sys.stderr'), self.assertRaises(SystemExit) as exit_status:
                lab.main()
            self.assertEqual(exit_status.exception.code, 2)


class ComposeBackendSelection(unittest.TestCase):
    def test_legacy_uses_explicit_file_and_clears_platform_override(self):
        with patch.object(lab, 'backend', return_value='legacy'), \
             patch.dict(os.environ, {'DOCKER_DEFAULT_PLATFORM': 'linux/amd64'}), \
             patch.object(lab.subprocess, 'run') as run:
            lab.run(['docker', 'compose', 'ps'])
        self.assertEqual(run.call_args.args[0], [
            'docker', 'compose', '-f', str(lab.ROOT / 'compose.legacy.yaml'), 'ps'])
        self.assertNotIn('DOCKER_DEFAULT_PLATFORM', run.call_args.kwargs['env'])
        self.assertEqual(run.call_args.kwargs['cwd'], lab.ROOT)

    def test_lima_uses_remote_compose_without_legacy_file(self):
        with patch.object(lab, 'backend', return_value='lima'), \
             patch.object(lab, 'settings', return_value={}), \
             patch.object(lab.subprocess, 'run') as run:
            lab.run(['docker', 'compose', 'ps'])
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ['limactl', 'shell', 'latch-arm64', '--'])
        self.assertIn('/home/stack/latch/compose.lima.yaml', command)
        self.assertNotIn(str(lab.ROOT / 'compose.legacy.yaml'), command)


class LabPreparation(unittest.TestCase):
    def test_lima_prepares_gateway_without_legacy_guest_seed(self):
        with tempfile.TemporaryDirectory(prefix='Latch POC ') as directory:
            state = Path(directory)
            with patch.object(lab, 'STATE', state), \
                 patch.object(lab, 'backend', return_value='lima'), \
                 patch.object(lab.warpgate, 'prepare') as prepare_gateway, \
                 patch.object(lab, 'run') as run:
                lab.prepare()
            prepare_gateway.assert_called_once_with(state)
            run.assert_not_called()
            self.assertEqual(state.stat().st_mode & 0o777, 0o700)
            for name in ('host_ed25519', 'user-data', 'meta-data'):
                self.assertFalse((state / name).exists())

    def test_legacy_preserves_existing_ssh_key_and_renders_cloud_init(self):
        with tempfile.TemporaryDirectory(prefix='Latch POC ') as directory:
            state = Path(directory)
            (state / 'host_ed25519').write_text('existing private key')
            (state / 'host_ed25519.pub').write_text('ssh-ed25519 existing-public-key\n')
            with patch.object(lab, 'STATE', state), \
                 patch.object(lab, 'backend', return_value='legacy'), \
                 patch.object(lab.warpgate, 'prepare') as prepare_gateway, \
                 patch.object(lab, 'run') as run:
                lab.prepare()
            run.assert_not_called()
            prepare_gateway.assert_called_once_with(state)
            self.assertEqual((state / 'host_ed25519').read_text(), 'existing private key')
            self.assertIn('ssh-ed25519 existing-public-key', (state / 'user-data').read_text())
            self.assertIn('instance-id: latch-openstack-001', (state / 'meta-data').read_text())


class LabSettings(unittest.TestCase):
    def test_environment_overrides_file_and_shared_port_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.env').write_text('LAB_API_PORT=19200\nLAB_MEMORY=12288\nLAB_BACKEND=legacy\n')
            with patch.object(lab, 'ROOT', root), \
                 patch.dict(os.environ, {'LAB_API_PORT': '29200', 'LAB_BACKEND': 'lima'}, clear=True):
                options = lab.settings()
        self.assertEqual(options['LAB_API_PORT'], '29200')
        self.assertEqual(options['LAB_BACKEND'], 'lima')
        self.assertEqual(options['LAB_MEMORY'], '12288')
        self.assertEqual(options['LAB_WORKER_PORT'], lab.lima.PORTS['LAB_WORKER_PORT'])

    def test_invalid_or_duplicate_ports_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(lab, 'ROOT', Path(directory)):
            for environment in ({'LAB_API_PORT': 'invalid'}, {'LAB_API_PORT': '80'},
                                {'LAB_API_PORT': '9202'}):
                with self.subTest(environment=environment), \
                     patch.dict(os.environ, environment, clear=True), self.assertRaises(ValueError):
                    lab.settings()


class LimaResume(unittest.TestCase):
    def test_up_restores_installed_services_after_provider_network(self):
        with patch.object(sys, 'argv', ['lab', 'up']), \
             patch.object(lab, 'prepare'), \
             patch.object(lab, 'backend', return_value='lima'), \
             patch.object(lab, 'settings', return_value={}), \
             patch.object(lab, 'retire_boundary'), \
             patch.object(lab.lima, 'start'), patch.object(lab, 'sync'), \
             patch.object(lab.server_integration, 'retire_legacy_api'), \
             patch.object(lab, 'run'), patch('builtins.print'), \
             patch.object(lab, 'guest', return_value=Mock(returncode=0)) as guest:
            lab.main()
        self.assertEqual(guest.call_args_list, [
            call('sudo env LAB_BAO_PORT=8200 python3 /home/stack/latch/guest/management-migrate.py prepare'),
            call('sudo env LAB_BAO_PORT=8200 python3 /home/stack/latch/guest/management-migrate.py verify'),
            call('test -f /opt/latch/install-complete', check=False),
            call('sudo systemctl restart latch-network'),
            call('sudo /bin/bash /home/stack/latch/guest/resume.sh'),
        ])


if __name__ == '__main__':
    unittest.main()
