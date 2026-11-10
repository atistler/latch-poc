"""Network lab state and isolation contracts; real protocol tests are opt-in."""
import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import network_integration as network

KEY = 'ssh-rsa ' + base64.b64encode(b'public-key-fixture').decode()


class NetworkIntegration(unittest.TestCase):
    def test_private_persistent_secret_and_existing_inventory_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            folder = network.prepare(ROOT, state)
            secret = (folder / 'tacacs-secret').read_bytes()
            config = {'devices': {'existing-router': {'host': '192.0.2.1'}},
                      'permissions': {'alice': {'existing-router': ['readonly']}, 'lab-operator': {'existing-router': ['operator']}}}
            (folder / 'inventory.json').write_text(json.dumps(config))
            network.merge_inventory(ROOT, state, KEY)
            network.merge_inventory(ROOT, state, KEY)
            actual = json.loads((folder / 'inventory.json').read_text())
            self.assertEqual(actual['devices']['existing-router'], config['devices']['existing-router'])
            self.assertEqual(actual['permissions']['alice'], config['permissions']['alice'])
            self.assertEqual(actual['permissions']['lab-operator']['existing-router'], ['operator'])
            self.assertEqual(actual['permissions']['lab-operator']['freertr'], ['readonly', 'operator', 'admin'])
            self.assertEqual(secret, (folder / 'tacacs-secret').read_bytes())
            self.assertEqual((folder / 'tacacs-secret').stat().st_mode & 0o777, 0o600)
            self.assertEqual((folder / 'inventory.json').stat().st_mode & 0o777, 0o600)
            before = (folder / 'inventory.json').read_bytes()
            with self.assertRaisesRegex(RuntimeError, 'review'):
                network.merge_inventory(ROOT, state, 'ssh-rsa ' + base64.b64encode(b'changed-key').decode())
            self.assertEqual(before, (folder / 'inventory.json').read_bytes())

    def test_existing_narrower_permissions_are_never_elevated(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            folder = network.prepare(ROOT, state)
            network.merge_inventory(ROOT, state, KEY)
            path = folder / 'inventory.json'
            config = json.loads(path.read_text())
            config['permissions']['lab-operator']['freertr'] = ['readonly']
            path.write_text(json.dumps(config))
            before = path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, 'permissions differ'):
                network.merge_inventory(ROOT, state, KEY)
            self.assertEqual(before, path.read_bytes())

    def test_generated_two_role_policy_migrates_once_with_private_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            folder = network.prepare(ROOT, state)
            path = folder / 'inventory.json'
            original = {'devices': {'freertr': {'host': 'freertr', 'port': 22,
                        'host_public_key': KEY, 'roles': ['readonly', 'operator']},
                        'other': {'host': '192.0.2.1'}},
                        'permissions': {'lab-operator': {'freertr': ['readonly', 'operator']},
                                        'alice': {'freertr': ['readonly']}}}
            path.write_text(json.dumps(original))
            before = path.read_bytes()
            network.merge_inventory(ROOT, state, KEY)
            backup = folder / 'inventory.before-admin.json'
            self.assertEqual(backup.read_bytes(), before)
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            updated = json.loads(path.read_text())
            self.assertEqual(updated['devices']['freertr']['roles'], ['readonly', 'operator', 'admin'])
            self.assertEqual(updated['permissions']['lab-operator']['freertr'], ['readonly', 'operator', 'admin'])
            self.assertEqual(updated['permissions']['alice'], original['permissions']['alice'])
            self.assertEqual(updated['devices']['other'], original['devices']['other'])
            network.merge_inventory(ROOT, state, KEY)
            self.assertEqual(backup.read_bytes(), before)
            self.assertEqual(json.loads(path.read_text()), updated)

    def test_custom_device_roles_are_not_expanded_or_backed_up(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            folder = network.prepare(ROOT, state)
            path = folder / 'inventory.json'
            original = {'devices': {'freertr': {'host': 'freertr', 'port': 22,
                        'host_public_key': KEY, 'roles': ['readonly']}},
                        'permissions': {'lab-operator': {'freertr': ['readonly', 'operator']}}}
            path.write_text(json.dumps(original))
            before = path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, 'inventory differs'):
                network.merge_inventory(ROOT, state, KEY)
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse((folder / 'inventory.before-admin.json').exists())

    def test_symlink_secret_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            folder = network.prepare(ROOT, state)
            (folder / 'tacacs-secret').unlink()
            (folder / 'tacacs-secret').symlink_to(folder / 'api-token')
            with self.assertRaises(RuntimeError):
                network.prepare(ROOT, state)

    def test_profiles_and_stop_preserve_foundation(self):
        command = network.compose(['ps'], {})
        self.assertEqual(command[:4], ['limactl', 'shell', 'latch-arm64', '--'])
        self.assertIn('/home/stack/latch/compose.lima.yaml', command)
        self.assertEqual(command.count('-f'), 1)
        self.assertEqual([command[i + 1] for i, arg in enumerate(command)
                          if arg == '--profile'], ['device', 'network'])
        with patch.object(network.subprocess, 'run') as run:
            network.down(ROOT, ROOT / '.state/lima', {})
        called = run.call_args.args[0]
        self.assertEqual(called[-4:], ['stop', 'freertr', 'tacacs', 'device-router'])
        self.assertNotIn('down', called)

    def test_sync_only_transfers_whitelisted_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            folder = network.prepare(ROOT, state)
            (folder / 'appliance.qcow2').write_bytes(b'not configuration')
            with patch.object(network.subprocess, 'run') as run:
                network.sync(ROOT, state)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertFalse(any('appliance.qcow2' in ' '.join(c) for c in commands))
            transfers = [call for call in run.call_args_list if 'input' in call.kwargs]
            self.assertEqual(len(transfers), 7)
            self.assertEqual(transfers[-1].kwargs['input'], (folder / 'tacacs-secret').read_bytes())


if __name__ == '__main__':
    unittest.main()
