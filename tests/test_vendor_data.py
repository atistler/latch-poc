"""Nova vendor data carries platform SSH trust without tenant user data."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class VendorData(unittest.TestCase):
    def test_cli_wraps_cloud_config_in_nova_vendor_data(self):
        with tempfile.TemporaryDirectory() as directory:
            ca = Path(directory) / 'ca.pub'
            ca.write_text('ssh-ed25519 test-ca\n')
            result = subprocess.check_output(
                ['python3', str(ROOT / 'guest/target-cloud-init.py'), str(ca)], text=True)
        vendor = json.loads(result)
        payload = vendor['cloud-init']
        self.assertTrue(payload.startswith('#cloud-config\n'))
        config = json.loads(payload.split('\n', 1)[1])
        files = {item['path']: item for item in config['write_files']}
        self.assertEqual(files['/etc/ssh/latch-user-ca.pub']['content'], 'ssh-ed25519 test-ca\n')
        self.assertIn('TrustedUserCAKeys', files['/etc/ssh/sshd_config.d/00-latch-certificates.conf']['content'])

    def test_provision_does_not_submit_user_data(self):
        provision = (ROOT / 'guest/provision.sh').read_text()
        self.assertNotIn('--user-data', provision)
        self.assertIn('vendor-data-configure.sh', provision)


if __name__ == '__main__':
    unittest.main()
