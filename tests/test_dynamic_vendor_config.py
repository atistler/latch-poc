"""Change only Nova's vendor-data options, preserving unrelated oslo config."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('vendor_data_config', ROOT / 'guest/vendor_data_config.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DynamicVendorConfig(unittest.TestCase):
    def test_preserves_comments_multivalue_options_and_other_sections(self):
        before = ('# operator comment\n[DEFAULT]\ntransport_url = rabbit://secret\n'
                  '[api]\n# keep this\nvendordata_providers = StaticJSON\n'
                  'vendordata_jsonfile_path = /etc/nova/old.json\n'
                  'enabled_apis = osapi_compute\nenabled_apis = metadata\n'
                  '[libvirt]\nvirt_type = kvm\n')
        after = module.configure_text(before)
        self.assertIn('# operator comment\n[DEFAULT]\ntransport_url = rabbit://secret\n', after)
        self.assertIn('# keep this\n', after)
        self.assertIn('enabled_apis = osapi_compute\nenabled_apis = metadata\n', after)
        self.assertTrue(after.endswith('[libvirt]\nvirt_type = kvm\n'))
        self.assertNotIn('vendordata_jsonfile_path', after)
        self.assertIn('vendordata_providers = StaticJSON,DynamicJSON\n', after)
        self.assertIn('cloud-init@http://127.0.0.1:8300/v1/vendor-data', after)
        self.assertEqual(module.configure_text(after), after)

    def test_adds_missing_api_section(self):
        before = '[DEFAULT]\nfoo = bar\n'
        after = module.configure_text(before)
        self.assertTrue(after.startswith(before))
        self.assertEqual(after.count('[api]'), 1)
        self.assertIn('vendordata_dynamic_failure_fatal = true', after)
        self.assertEqual(module.configure_text(after), after)

    def test_removes_duplicate_vendor_settings_and_multiline_continuations(self):
        before = ('[api]\nvendordata_dynamic_targets = old@http://old\n'
                  '    more@http://old2\nother = keep\n[api]\n'
                  'vendordata_providers=StaticJSON\n[other]\nvendordata_providers=untouched\n')
        after = module.configure_text(before)
        self.assertNotIn('http://old', after)
        self.assertIn('other = keep\n', after)
        self.assertIn('[other]\nvendordata_providers=untouched\n', after)
        self.assertEqual(after.count('vendordata_providers = StaticJSON,DynamicJSON'), 1)
        self.assertEqual(module.configure_text(after), after)

    def test_protected_backup_survives_repeated_configure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'nova.conf'
            path.write_text('[DEFAULT]\nsecret = original\n')
            path.chmod(0o640)
            backup = Path(directory) / 'backup' / 'nova.conf'
            self.assertTrue(module.configure_file(path, backup))
            self.assertEqual(backup.read_text(), '[DEFAULT]\nsecret = original\n')
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            self.assertEqual(backup.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            self.assertFalse(module.configure_file(path, backup))
            self.assertEqual(backup.read_text(), '[DEFAULT]\nsecret = original\n')

    def run_wrapper(self, directory, ready=0, changed=0, restart=0, cpu_config=False):
        directory = Path(directory)
        binary = directory / 'bin'
        binary.mkdir(exist_ok=True)
        for command, code in [('curl', ready), ('python3', changed), ('systemctl', restart)]:
            path = binary / command
            path.write_text(f'#!/bin/sh\necho {command} >> "$EVENTS"\nexit {code}\n')
            path.chmod(0o755)
        script = (ROOT / 'guest/vendor-data-configure.sh').read_text()
        script = script.replace('/run/latch-vendor-data-restart', str(directory / 'restart'))
        if cpu_config:
            cpu_path = directory / 'nova-cpu.conf'
            cpu_path.touch()
            script = script.replace('/etc/nova/nova-cpu.conf', str(cpu_path))
        events = directory / 'events'
        result = subprocess.run(['bash', '-c', script], capture_output=True, text=True,
                                env={**os.environ, 'PATH': f'{binary}:/usr/bin:/bin', 'EVENTS': str(events)})
        return result, events.read_text().splitlines()

    def test_unready_api_does_not_mutate_or_restart_nova(self):
        with tempfile.TemporaryDirectory() as directory:
            result, events = self.run_wrapper(directory, ready=22)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(events, ['curl'])

    def test_unchanged_settings_do_not_restart_services(self):
        with tempfile.TemporaryDirectory() as directory:
            result, events = self.run_wrapper(directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(events, ['curl', 'python3'])

    def test_devstack_compute_config_is_updated_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            result, events = self.run_wrapper(directory, cpu_config=True, changed=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(events, ['curl', 'python3', 'python3', 'systemctl'])

    def test_failed_restart_is_retried_even_if_config_is_already_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            result, events = self.run_wrapper(directory, changed=10, restart=1)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(events, ['curl', 'python3', 'systemctl'])
            self.assertTrue((Path(directory) / 'restart').exists())
            (Path(directory) / 'events').unlink()
            result, events = self.run_wrapper(directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(events, ['curl', 'python3', 'systemctl'])
            self.assertFalse((Path(directory) / 'restart').exists())


if __name__ == '__main__':
    unittest.main()
