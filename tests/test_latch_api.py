"""Latch HTTP contract, using an OpenBao HTTP transport rather than CA secrets."""
import importlib.util
import asyncio
import json
import time
import unittest

AVAILABLE = all(importlib.util.find_spec(name) for name in ('fastapi', 'httpx', 'cryptography'))
if AVAILABLE:
    import httpx
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from fastapi.testclient import TestClient
    from latch_api.app import create_app
    from latch_api.openbao import OpenBaoClient


@unittest.skipUnless(AVAILABLE, 'Install latch_api/requirements.txt to run Latch API contract tests')
class LatchAPI(unittest.TestCase):
    def setUp(self):
        self.ca = Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
        self.context = {'instance-id': '13fdcc3d-0b86-4cc7-a460-13143ea2267e',
                        'project-id': 'd0c0b9f5142647029ee71c485adc7490',
                        'image-id': 'bfdf2d3c-e1ad-4218-bcad-5cd1f950a2df',
                        'hostname': 'latch-vendor-test', 'metadata': {}, 'user-data': None}
        self.requests = []

    def client(self, response=None, failure=None, revision='openbao-v1'):
        def handler(request):
            self.requests.append(request)
            if failure:
                raise failure
            return response or httpx.Response(200, text=self.ca)
        bao = OpenBaoClient('http://openbao:8200', transport=httpx.MockTransport(handler))
        return TestClient(create_app(bao=bao, profile_revision=revision))

    def test_response_becomes_cloud_init_vendor_data_without_extra_wrapper(self):
        response = self.client().post('/v1/vendor-data', json=self.context)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIsInstance(payload, str)
        self.assertTrue(payload.startswith('#cloud-config\n'))
        vendor_data2 = {'cloud-init': payload}
        config = json.loads(vendor_data2['cloud-init'].split('\n', 1)[1])
        files = {item['path']: item['content'] for item in config['write_files']}
        self.assertEqual(files['/etc/ssh/latch-user-ca.pub'], self.ca + '\n')
        self.assertIn('AuthorizedKeysFile none', files['/etc/ssh/sshd_config.d/00-latch-certificates.conf'])
        self.assertIn('NTP=100.64.10.1', files['/etc/systemd/timesyncd.conf.d/latch.conf'])
        self.assertEqual(json.loads(files['/etc/latch/bootstrap.json'])['profile_revision'], 'openbao-v1')
        self.assertEqual(str(self.requests[0].url), 'http://openbao:8200/v1/ssh-client-signer/public_key')
        self.assertNotIn('x-vault-token', self.requests[0].headers)
        self.assertNotIn('authorization', self.requests[0].headers)

    def test_invalid_identifiers_are_rejected_before_openbao_request(self):
        for field in ('instance-id', 'project-id'):
            with self.subTest(field=field):
                request = dict(self.context, **{field: '../../config'})
                self.assertEqual(self.client().post('/v1/vendor-data', json=request).status_code, 422)
        self.assertEqual(self.requests, [])

    def test_unrecognized_fields_and_tenant_input_cannot_override_policy(self):
        client = self.client()
        baseline = client.post('/v1/vendor-data', json=self.context).json()
        request = dict(self.context, metadata={'ssh-ca': 'attacker', 'profile': 'root'},
                       **{'user-data': '#cloud-config\nssh_pwauth: true', 'boot-roles': 'admin'})
        self.assertEqual(client.post('/v1/vendor-data', json=request).json(), baseline)
        self.assertEqual(client.post('/v1/vendor-data', json=request).json(), baseline)

    def test_missing_optional_context_is_accepted(self):
        request = {key: self.context[key] for key in ('instance-id', 'project-id')}
        self.assertEqual(self.client().post('/v1/vendor-data', json=request).status_code, 200)

    def test_health_readiness_requires_valid_ca_but_liveness_does_not(self):
        client = self.client(response=httpx.Response(503, text='sealed'))
        self.assertEqual(client.get('/health/live').status_code, 200)
        self.assertEqual(self.requests, [])
        self.assertEqual(client.get('/health/ready').status_code, 503)

    def test_upstream_failures_do_not_return_empty_successful_configuration(self):
        cases = [self.client(response=httpx.Response(503, text='sealed')),
                 self.client(response=httpx.Response(200, text='ssh-ed25519 invalid')),
                 self.client(response=httpx.Response(200, text='')),
                 self.client(failure=httpx.ConnectError('unavailable')),
                 self.client(failure=httpx.ReadTimeout('timeout'))]
        for client in cases:
            with self.subTest(client=client):
                result = client.post('/v1/vendor-data', json=self.context)
                self.assertEqual(result.status_code, 503)
                self.assertEqual(result.json(), {'detail': 'SSH CA unavailable'})

    def test_private_material_and_large_payloads_are_rejected(self):
        for body in ('-----BEGIN OPENSSH PRIVATE KEY-----', self.ca + '\n' + self.ca, 'x' * 8193):
            with self.subTest(body=body[:40]):
                result = self.client(response=httpx.Response(200, text=body)).post(
                    '/v1/vendor-data', json=self.context)
                self.assertEqual(result.status_code, 503)

    def test_profile_revision_is_operator_configured(self):
        response = self.client(revision='profile-v2').post('/v1/vendor-data', json=self.context)
        config = json.loads(response.json().split('\n', 1)[1])
        marker = next(item for item in config['write_files'] if item['path'] == '/etc/latch/bootstrap.json')
        self.assertEqual(json.loads(marker['content'])['profile_revision'], 'profile-v2')

    def test_total_upstream_budget_limits_slow_responses(self):
        async def slow(request):
            await asyncio.sleep(10)
            return httpx.Response(200, text=self.ca)
        bao = OpenBaoClient('http://openbao:8200', transport=httpx.MockTransport(slow))
        client = TestClient(create_app(bao=bao))
        started = time.monotonic()
        response = client.post('/v1/vendor-data', json=self.context)
        self.assertEqual(response.status_code, 503)
        self.assertLess(time.monotonic() - started, 3)

    def test_audit_log_records_delivery_without_tenant_payload(self):
        request = dict(self.context, metadata={'private-value': 'tenant-secret'},
                       **{'user-data': 'user-data-secret'})
        with self.assertLogs('uvicorn.error', level='INFO') as captured:
            self.assertEqual(self.client().post('/v1/vendor-data', json=request).status_code, 200)
        event = json.loads(captured.records[0].getMessage())
        self.assertEqual(event['instance_id'], self.context['instance-id'])
        self.assertEqual(event['outcome'], 'served')
        self.assertTrue(event['ssh_ca_fingerprint'].startswith('SHA256:'))
        self.assertNotIn('tenant-secret', ''.join(captured.output))
        self.assertNotIn('user-data-secret', ''.join(captured.output))


if __name__ == '__main__':
    unittest.main()
