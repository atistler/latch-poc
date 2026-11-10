"""KVM capability failures must select the documented Nova backend safely."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, call, mock_open, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('acceleration', ROOT / 'guest/acceleration.py')
acceleration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acceleration)


class AccelerationSelection(unittest.TestCase):
    def test_auto_uses_kvm_only_after_probe_succeeds(self):
        with patch.object(acceleration, 'probe_kvm') as probe:
            virt, reason = acceleration.select('auto')
        probe.assert_called_once_with()
        self.assertEqual(virt, 'kvm')
        self.assertIn('initialization succeeded', reason)

    def test_auto_falls_back_for_missing_device_and_failed_vcpu(self):
        for error in (FileNotFoundError('/dev/kvm'), OSError(22, 'Invalid argument')):
            with self.subTest(error=error), patch.object(acceleration, 'probe_kvm', side_effect=error):
                virt, reason = acceleration.select('auto')
            self.assertEqual(virt, 'qemu')
            self.assertEqual(reason, str(error))

    def test_explicit_kvm_fails_instead_of_falling_back(self):
        with patch.object(acceleration, 'probe_kvm', side_effect=PermissionError('denied')):
            with self.assertRaisesRegex(RuntimeError, 'KVM was requested but is unavailable: denied'):
                acceleration.select('kvm')

    def test_explicit_tcg_never_accesses_kvm(self):
        with patch.object(acceleration, 'probe_kvm') as probe:
            self.assertEqual(acceleration.select('tcg')[0], 'qemu')
        probe.assert_not_called()

    def test_invalid_mode_never_probes(self):
        with patch.object(acceleration, 'probe_kvm') as probe:
            with self.assertRaises(ValueError):
                acceleration.select('accelerated')
        probe.assert_not_called()


class KvmProbe(unittest.TestCase):
    def test_probe_initializes_arm_cpu_and_closes_both_descriptors(self):
        device = MagicMock()
        opened = mock_open()
        opened.return_value.__enter__.return_value = device
        with patch('builtins.open', opened), \
             patch.object(acceleration.fcntl, 'ioctl', side_effect=[12, 41, 0, 42, 0]) as ioctl, \
             patch.object(acceleration.os, 'close') as close:
            acceleration.probe_kvm()
        self.assertEqual([item.args[1] for item in ioctl.call_args_list],
                         [0xAE00, 0xAE01, 0x8020AEAF, 0xAE41, 0x4020AEAE])
        self.assertEqual(len(ioctl.call_args_list[2].args[2]), 32)
        self.assertIs(ioctl.call_args_list[2].args[2], ioctl.call_args_list[4].args[2])
        self.assertEqual(close.call_args_list, [call(42), call(41)])

    def test_initialization_failure_still_closes_both_descriptors(self):
        with patch('builtins.open', mock_open()), \
             patch.object(acceleration.fcntl, 'ioctl', side_effect=[12, 41, 0, 42, OSError('no EL2')]), \
             patch.object(acceleration.os, 'close') as close:
            with self.assertRaisesRegex(OSError, 'no EL2'):
                acceleration.probe_kvm()
        self.assertEqual(close.call_args_list, [call(42), call(41)])

    def test_preferred_target_failure_closes_vm_before_fallback(self):
        with patch('builtins.open', mock_open()), \
             patch.object(acceleration.fcntl, 'ioctl', side_effect=[12, 41, OSError('unsupported')]), \
             patch.object(acceleration.os, 'close') as close:
            self.assertEqual(acceleration.select('auto')[0], 'qemu')
        close.assert_called_once_with(41)


class NovaConfiguration(unittest.TestCase):
    def render(self, virt):
        files = {}

        def path(name):
            file = files.setdefault(str(name), MagicMock())
            file.exists.return_value = False
            file.read_text.return_value = (ROOT / 'config/openstack/local.conf').read_text()
            return file

        with patch.object(acceleration, 'Path', side_effect=path), \
             patch.object(acceleration.platform, 'machine', return_value='aarch64'), \
             patch.object(acceleration, 'select', return_value=(virt, 'test reason')), \
             patch.object(sys, 'argv', ['acceleration.py', 'auto']), \
             patch('builtins.print'):
            acceleration.main()
        config = files['/opt/stack/devstack/local.conf'].write_text.call_args.args[0]
        report = json.loads(files['/opt/latch/acceleration.json'].write_text.call_args.args[0])
        self.assertEqual(report['virt_type'], virt)
        self.assertEqual(report['architecture'], 'aarch64')
        return config

    def test_kvm_has_no_emulated_cpu_settings(self):
        config = self.render('kvm')
        self.assertIn('LIBVIRT_TYPE=kvm', config)
        self.assertIn('LIBVIRT_CPU_MODE=host-passthrough', config)
        self.assertIn('virt_type=kvm', config)
        self.assertIn('cpu_mode=host-passthrough', config)
        self.assertNotIn('virt_type=qemu', config)
        self.assertNotIn('cpu_models=', config)

    def test_tcg_uses_arm_max_cpu(self):
        config = self.render('qemu')
        self.assertIn('LIBVIRT_TYPE=qemu', config)
        self.assertIn('LIBVIRT_CPU_MODE=custom', config)
        self.assertIn('LIBVIRT_CPU_MODEL=max', config)
        self.assertIn('cpu_models=max', config)
        self.assertNotIn('host-passthrough', config)

    def test_installed_accelerator_change_does_not_rewrite_config_or_report(self):
        files = {}

        def path(name):
            file = files.setdefault(str(name), MagicMock())
            file.exists.return_value = True
            file.read_text.return_value = json.dumps({'virt_type': 'kvm'})
            return file

        with patch.object(acceleration, 'Path', side_effect=path), \
             patch.object(acceleration.platform, 'machine', return_value='aarch64'), \
             patch.object(acceleration, 'select', return_value=('qemu', 'KVM unavailable')), \
             patch.object(sys, 'argv', ['acceleration.py', 'auto']):
            with self.assertRaisesRegex(RuntimeError, 'explicit Nova migration/rebuild'):
                acceleration.main()
        for file in files.values():
            file.write_text.assert_not_called()


if __name__ == '__main__':
    unittest.main()
