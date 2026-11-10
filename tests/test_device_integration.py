"""Credential and optional-stack isolation contracts without creating a VM."""
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import device_integration as device


class Preparation(unittest.TestCase):
    def test_generated_secret_bytes_are_private_stable_and_have_no_newline(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            folder = device.prepare(root, state)
            before = {p.name: p.read_bytes() for p in folder.iterdir()}
            device.prepare(root, state)
            self.assertEqual(before, {p.name: p.read_bytes() for p in folder.iterdir()})
            for path in folder.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                if path.name != 'inventory.json':
                    self.assertNotIn(b'\n', path.read_bytes())
                    self.assertGreaterEqual(len(path.read_bytes()), 32)

    def test_previous_state_migration_preserves_credentials_and_never_overwrites(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            previous = state / 'ise'
            previous.mkdir()
            originals = {}
            for name in (*device.SECRET_FILES, 'inventory.json', 'tacacs-secret'):
                old_name = 'ldap-ise-password' if name == 'ldap-reader-password' else name
                value = b'{"devices":{},"permissions":{}}' if name == 'inventory.json' else (name + '-original-password').encode()
                (previous / old_name).write_bytes(value)
                originals[name] = value
            (previous / 'unrelated.qcow2').write_bytes(b'not-configuration')
            folder = device.prepare(root, state)
            self.assertEqual(folder, state / 'device')
            for name, value in originals.items():
                self.assertEqual((folder / name).read_bytes(), value)
                self.assertEqual((folder / name).stat().st_mode & 0o777, 0o600)
            self.assertFalse((folder / 'unrelated.qcow2').exists())
            (folder / 'api-token').write_bytes(b'new-authoritative-token')
            device.prepare(root, state)
            self.assertEqual((folder / 'api-token').read_bytes(), b'new-authoritative-token')
            self.assertEqual((previous / 'api-token').read_bytes(), originals['api-token'])

    def test_profile_targets_lima_and_does_not_publish_new_ports(self):
        command = device.compose(['ps'], {})
        self.assertEqual(command[:4], ['limactl', 'shell', 'latch-arm64', '--'])
        self.assertIn('/home/stack/latch/compose.lima.yaml', command)
        self.assertEqual(command.count('-f'), 1)
        self.assertEqual(command[command.index('--profile') + 1], 'device')
        self.assertNotIn('-p', command)

    def test_sync_whitelists_configuration_and_ignores_appliance_images(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            folder = device.prepare(root, state)
            (folder / 'device.qcow2').write_bytes(b'not-configuration')
            with patch.object(device.subprocess, 'run') as run:
                device.sync(root, state)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertFalse(any('device.qcow2' in ' '.join(c) for c in commands))
            transferred = [call.kwargs['input'] for call in run.call_args_list if 'input' in call.kwargs]
            self.assertEqual(len(transferred), 6)


class ServiceMigration(unittest.TestCase):
    def test_old_writer_stops_before_replacement_and_after_successful_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / 'warpgate').mkdir()
            (state / 'warpgate/tls.certificate.pem').write_text('existing-pin')
            def result(command, **kwargs):
                return Mock(stdout='old-container-id\n' if '{{.ID}}' in command else '')
            with patch.object(device, 'sync'), \
                 patch.object(device.subprocess, 'run', side_effect=result) as run:
                device.up(state, state, {})
            commands = [call.args[0] for call in run.call_args_list]
            stopped = next(i for i, command in enumerate(commands)
                           if command[-2:] == ['stop', 'latch-poc-ise-openbao-1'])
            built = next(i for i, command in enumerate(commands) if 'build' in command)
            started = next(i for i, command in enumerate(commands) if 'up' in command)
            self.assertLess(built, stopped)
            self.assertLess(stopped, started)
            listing = next(command for command in commands if '{{.ID}}' in command)
            self.assertIn('name=^/latch-poc-ise-openbao-1$', listing)
            self.assertIn('label=com.docker.compose.project=latch-poc', listing)
            self.assertIn('label=com.docker.compose.service=ise-openbao', listing)

    def test_missing_old_service_never_stops_any_container(self):
        with patch.object(device.subprocess, 'run', return_value=Mock(stdout='')) as run:
            device.stop_previous_openbao()
        self.assertEqual(run.call_count, 1)

    def test_failed_stop_blocks_replacement_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / 'warpgate').mkdir()
            (state / 'warpgate/tls.certificate.pem').write_text('existing-pin')
            def result(command, **kwargs):
                if 'stop' in command:
                    raise subprocess.CalledProcessError(1, command)
                return Mock(stdout='old-container-id\n' if '{{.ID}}' in command else '')
            with patch.object(device, 'sync'), \
                 patch.object(device.subprocess, 'run', side_effect=result) as run, \
                 self.assertRaises(subprocess.CalledProcessError):
                device.up(state, state, {})
            self.assertFalse(any('up' in call.args[0] for call in run.call_args_list))


if __name__ == '__main__':
    unittest.main()
