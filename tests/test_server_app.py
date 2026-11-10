"""Authenticated Server grant HTTP boundary and expiry worker."""
import importlib.util
import time
import unittest
from unittest.mock import Mock

AVAILABLE = all(importlib.util.find_spec(name) for name in ('fastapi', 'httpx', 'cryptography'))
if AVAILABLE:
    from fastapi.testclient import TestClient
    from latch_api.device_access import AccessError
    from latch_api.server_app import create_app


@unittest.skipUnless(AVAILABLE, 'Install latch_api/requirements.txt')
class ServerHTTP(unittest.TestCase):
    token = 'test-token-' + 'x' * 40

    def setUp(self):
        self.access = Mock()
        self.instance = '11111111-1111-1111-1111-111111111111'
        self.grant_id = '22222222-2222-2222-2222-222222222222'
        self.grant = {'grant_id': self.grant_id, 'server_id': self.instance, 'state': 'active',
                      'expires_at': '2030-01-01T00:00:00Z', 'gateway_host': '127.0.0.1',
                      'gateway_port': 9202, 'gateway_host_public_key': 'ssh-ed25519 example',
                      'ssh_username': 'latch-server-example:latch-server-example'}
        self.access.issue.return_value = self.grant
        self.access.get.return_value = self.grant
        self.access.revoke.return_value = dict(self.grant, state='revoked')
        self.headers = {'Authorization': 'Bearer ' + self.token}
        self.payload = {'server_id': self.instance, 'public_key': 'ssh-ed25519 ' + 'A' * 40}

    def client(self, **kwargs):
        return TestClient(create_app(self.access, self.token, **kwargs))

    def test_all_grant_operations_require_bearer_before_coordinator(self):
        with self.client() as client:
            for method, path, data in [('POST', '/v1/server-grants', self.payload),
                                       ('GET', '/v1/server-grants/' + self.grant_id, None),
                                       ('DELETE', '/v1/server-grants/' + self.grant_id, None)]:
                self.assertEqual(client.request(method, path, json=data).status_code, 401)
            self.assertEqual(client.get('/health/live').status_code, 200)
        self.access.issue.assert_not_called()
        self.access.get.assert_not_called()
        self.access.revoke.assert_not_called()

    def test_identity_is_server_controlled_and_retry_key_forwarded(self):
        with self.client() as client:
            result = client.post('/v1/server-grants', headers=dict(self.headers, **{'Idempotency-Key': 'retry-1'}),
                                 json=self.payload)
            self.assertEqual(result.status_code, 201)
            self.assertEqual(result.json(), self.grant)
            self.access.issue.assert_called_once_with('lab-operator', self.instance,
                                                     self.payload['public_key'], 300, 'retry-1')
            for field, value in [('caller', 'admin'), ('host', 'attacker'), ('role', 'admin')]:
                result = client.post('/v1/server-grants', headers=self.headers,
                                     json=dict(self.payload, **{field: value}))
                self.assertEqual(result.status_code, 422)

    def test_ownership_and_dependency_errors_are_preserved(self):
        self.access.get.side_effect = AccessError('Grant not found', 404)
        self.access.issue.side_effect = AccessError('Server access provisioning failed; cleanup scheduled')
        with self.client() as client:
            self.assertEqual(client.get('/v1/server-grants/' + self.grant_id,
                                        headers=self.headers).status_code, 404)
            response = client.post('/v1/server-grants', json=self.payload, headers=self.headers)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), {'detail': 'Server access provisioning failed; cleanup scheduled'})

    def test_validated_identifiers_ttl_and_no_arbitrary_endpoints(self):
        with self.client() as client:
            for update in ({'server_id': '../bad'}, {'ttl_seconds': 301},
                           {'ttl_seconds': True}, {'ttl_seconds': '30'}, {'host': '127.0.0.1'}):
                self.assertEqual(client.post('/v1/server-grants', json=dict(self.payload, **update),
                                             headers=self.headers).status_code, 422)
            self.assertEqual(client.get('/v1/server-grants/not-a-uuid', headers=self.headers).status_code, 422)
        self.access.issue.assert_not_called()

    def test_revoke_returns_pending_honestly(self):
        self.access.revoke.return_value = dict(self.grant, state='cleanup_pending')
        with self.client() as client:
            response = client.delete('/v1/server-grants/' + self.grant_id, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['state'], 'cleanup_pending')

    def test_documentation_has_bearer_security_without_runtime_credentials(self):
        with self.client() as client:
            self.assertEqual(client.get('/docs').status_code, 200)
            self.assertIn('<redoc', client.get('/docs').text)
            self.assertEqual(client.get('/openapi.yaml').status_code, 200)
            schema = client.get('/openapi.json').json()
        for path, method in [('/v1/server-grants', 'post'), ('/v1/server-grants/{grant_id}', 'get'),
                             ('/v1/server-grants/{grant_id}', 'delete')]:
            self.assertEqual(schema['paths'][path][method]['security'], [{'LatchBearer': []}])
        self.assertNotIn(self.token, str(schema))
        self.access.issue.assert_not_called()

    def test_expiry_worker_retries_after_sanitized_failure(self):
        self.access.sweep.side_effect = [0, RuntimeError('upstream-private-secret'), 1, 0, 0, 0]
        with self.assertLogs('uvicorn.error', level='ERROR') as logs:
            with self.client(sweep_interval=0.02):
                deadline = time.monotonic() + 1
                while self.access.sweep.call_count < 3 and time.monotonic() < deadline:
                    time.sleep(0.01)
        self.assertGreaterEqual(self.access.sweep.call_count, 3)
        self.assertNotIn('upstream-private-secret', ''.join(logs.output))


if __name__ == '__main__':
    unittest.main()
