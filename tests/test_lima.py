"""Backend safety and capability selection, without creating a VM."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import lima_backend as lima


class LimaConfiguration(unittest.TestCase):
    def test_m1_m2_disable_nested_and_m4_enables_it(self):
        for chip, version, expected in [('Apple M1', '15.3.2', False),
                                        ('Apple M2 Pro', '15.3', False),
                                        ('Apple M4', '15.3.2', True),
                                        ('Apple M4', '14.7', False)]:
            config = lima.configuration({}, chip, version)
            self.assertEqual(config['nestedVirtualization'], expected)
            self.assertEqual(config['arch'], 'aarch64')
            self.assertEqual(config['vmType'], 'vz')

    def test_explicit_tcg_and_unavailable_kvm(self):
        self.assertFalse(lima.configuration({'LAB_ACCEL': 'tcg'}, 'Apple M4', '15.3')['nestedVirtualization'])
        with self.assertRaisesRegex(ValueError, 'M3'):
            lima.configuration({'LAB_ACCEL': 'kvm'}, 'Apple M1', '15.3')
        with self.assertRaises(ValueError):
            lima.configuration({'LAB_ACCEL': 'typo'}, 'Apple M4', '15.3')

    def test_remote_compose_does_not_inherit_desktop_socket_or_platform(self):
        command = lima.compose_command(['ps'], {'LAB_API_PORT': '19200'})
        self.assertEqual(command[:4], ['limactl', 'shell', 'latch-arm64', '--'])
        self.assertIn('DOCKER_HOST', command)
        self.assertIn('DOCKER_DEFAULT_PLATFORM', command)
        self.assertIn('LAB_API_PORT=19200', command)
        self.assertIn('/home/stack/latch/compose.lima.yaml', command)

    def test_only_requested_loopback_ports_are_forwarded(self):
        config = lima.configuration({}, 'Apple M1', '15.3')
        self.assertEqual([p['guestPort'] for p in config['portForwards'][:-1]], [9200, 9202, 8200])
        self.assertEqual(len(config['portForwards']), 4)
        self.assertTrue(all(p['hostIP'] == '127.0.0.1' and not p.get('ignore', False)
                            for p in config['portForwards'][:-1]))
        # guestIP alone only suppresses wildcard binds. Without the explicit
        # false flag, Lima's implicit fallback still publishes localhost ports.
        self.assertEqual(config['portForwards'][-1], {
            'guestIP': '0.0.0.0', 'guestIPMustBeZero': False, 'proto': 'any',
            'guestPortRange': [1, 65535], 'ignore': True,
        })
        self.assertEqual(json.loads(json.dumps(config))['mounts'], [])

    def test_custom_port_forward_matches_compose_published_port(self):
        options = {'LAB_API_PORT': '19200', 'LAB_WORKER_PORT': '19202', 'LAB_BAO_PORT': '18200'}
        config = lima.configuration(options, 'Apple M4', '15.3')
        forwards = config['portForwards'][:-1]
        self.assertEqual([p['guestPort'] for p in forwards], [19200, 19202, 18200])
        self.assertEqual([p['hostPort'] for p in forwards], [19200, 19202, 18200])
        self.assertTrue(all(p['hostIP'] == '127.0.0.1' for p in forwards))

    def test_invalid_resources_fail_before_vm_creation(self):
        for options in ({'LAB_MEMORY': '0'}, {'LAB_MEMORY': 'bad'},
                        {'LAB_CPUS': '0'}, {'LAB_CPUS': '2; touch /tmp/bad'},
                        {'LAB_DISK': '80'}, {'LAB_DISK': '80G; touch /tmp/bad'}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                lima.configuration(options, 'Apple M4', '15.3')

    def test_custom_resources_are_native_vm_units(self):
        config = lima.configuration({'LAB_MEMORY': '12288', 'LAB_CPUS': '8', 'LAB_DISK': '100G'},
                                    'Apple M4', '15.3')
        self.assertEqual(config['memory'], '12288MiB')
        self.assertEqual(config['cpus'], 8)
        self.assertEqual(config['disk'], '100GiB')


class LimaExistingVm(unittest.TestCase):
    def start_existing(self, state):
        listing = Mock(stdout=json.dumps({'name': lima.NAME}) + '\n')
        with patch.object(lima.platform, 'system', return_value='Darwin'), \
             patch.object(lima.platform, 'machine', return_value='arm64'), \
             patch.object(lima.platform, 'mac_ver', return_value=('15.3', (), '')), \
             patch.object(lima.shutil, 'which', return_value='/opt/homebrew/bin/limactl'), \
             patch.object(lima.subprocess, 'check_output', return_value='Apple M1\n'), \
             patch.object(lima.subprocess, 'run', return_value=listing) as run, \
             patch('builtins.print'):
            try:
                lima.start(state.parent, state, {})
            finally:
                self.commands = run.call_args_list

    def test_unregistered_existing_vm_is_never_started(self):
        with tempfile.TemporaryDirectory(prefix='latch test ') as directory:
            with self.assertRaisesRegex(RuntimeError, 'not registered'):
                self.start_existing(Path(directory))
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.commands[0].args[0], ['limactl', 'list', '--json'])

    def test_changed_vm_settings_are_rejected_before_start(self):
        with tempfile.TemporaryDirectory(prefix='latch test ') as directory:
            state = Path(directory)
            config = lima.configuration({'LAB_CPUS': '8'}, 'Apple M1', '15.3')
            (state / 'lima.yaml').write_text(json.dumps(config))
            with self.assertRaisesRegex(RuntimeError, 'settings changed'):
                self.start_existing(state)
        self.assertEqual(len(self.commands), 1)

    def test_matching_recorded_vm_can_restart(self):
        with tempfile.TemporaryDirectory(prefix='latch test ') as directory:
            state = Path(directory)
            (state / 'lima.yaml').write_text(json.dumps(lima.configuration({}, 'Apple M1', '15.3')))
            self.start_existing(state)
        self.assertEqual(self.commands[1].args[0], ['limactl', 'start', lima.NAME, '--tty=false'])


if __name__ == '__main__':
    unittest.main()
