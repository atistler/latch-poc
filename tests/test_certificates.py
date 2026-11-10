"""Real OpenBao + OpenSSH integration checks; run via Dockerfile.certificates."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from certificates import BaoError, api, issue, login

ADDRESS = 'http://127.0.0.1:8200'


@unittest.skipUnless(os.environ.get('LATCH_CERTIFICATE_INTEGRATION') == '1',
                     'Run via tests/Dockerfile.certificates for real OpenBao and SSH services')
class CertificateIntegration(unittest.TestCase):
    def test_signing_and_ssh(self):
        with tempfile.TemporaryDirectory() as state:
            key, cert = issue(ADDRESS, state)
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            details = subprocess.check_output(['ssh-keygen', '-L', '-f', str(cert)], text=True)
            self.assertIn('ubuntu', details)
            token = login(ADDRESS)
            with self.assertRaises(BaoError) as denied:
                api(ADDRESS, 'ssh-client-signer/roles/escape', {'key_type': 'ca'}, token)
            self.assertEqual(denied.exception.status, 403)
            with self.assertRaises(BaoError) as denied:
                api(ADDRESS, 'sys/mounts/escape', {'type': 'kv'}, token)
            self.assertEqual(denied.exception.status, 403)
            for principal, ttl in [('root', '15m'), ('ubuntu', '1h')]:
                with self.subTest(principal=principal, ttl=ttl):
                    with self.assertRaises(BaoError) as denied:
                        issue(ADDRESS, state, principal=principal, ttl=ttl, name='invalid.pub')
                    self.assertEqual(denied.exception.status, 400)

            def ssh(certificate, user='ubuntu'):
                return subprocess.run([
                    'ssh', '-p', '2223', '-i', str(key), '-o', f'CertificateFile={certificate or "none"}',
                    '-o', 'IdentityAgent=none', '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
                    '-o', 'StrictHostKeyChecking=accept-new', '-o', f'UserKnownHostsFile={state}/hosts',
                    f'{user}@127.0.0.1', 'whoami'], capture_output=True, text=True, timeout=15)

            valid = ssh(cert)
            self.assertEqual(valid.returncode, 0, valid.stderr)
            self.assertEqual(valid.stdout.strip(), 'ubuntu')
            for label, certificate, user in [('unsigned', None, 'ubuntu'), ('wrong-principal', cert, 'latch-other')]:
                with self.subTest(label=label):
                    rejected = ssh(certificate, user)
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertIn('Permission denied (publickey)', rejected.stderr)
            _, expired = issue(ADDRESS, state, ttl='1s', name='expired.pub')
            time.sleep(3)
            rejected = ssh(expired)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn('Permission denied (publickey)', rejected.stderr)
            self.assertEqual(ssh(cert).returncode, 0)
            print('PASS: valid certificate accepted; unsigned, expired and wrong-principal credentials rejected')


if __name__ == '__main__':
    unittest.main(verbosity=2)
