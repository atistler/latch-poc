"""The isolated tenant must synchronize time before using short-lived certificates."""
import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1] / 'guest/target-cloud-init.py'
spec = importlib.util.spec_from_file_location('target_cloud_init', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class TenantTime(unittest.TestCase):
    def test_isolated_tenant_uses_local_ntp_and_can_correct_sleep_drift(self):
        config = module.cloud_config('ssh-ed25519 test-ca')
        files = {entry['path']: entry['content'] for entry in config['write_files']}
        time_config = files.get('/etc/systemd/timesyncd.conf.d/latch.conf', '')
        self.assertIn('NTP=100.64.10.1', time_config)
        self.assertIn('FallbackNTP=\n', time_config)
        self.assertIn('PollIntervalMaxSec=32', time_config)
        commands = config['runcmd']
        self.assertIn(['systemctl', 'enable', '--now', 'systemd-timesyncd'], commands)
        self.assertLess(commands.index(['systemctl', 'restart', 'systemd-timesyncd']),
                        commands.index(['systemctl', 'restart', 'ssh']))


if __name__ == '__main__':
    unittest.main()
