"""Authenticated HTTP boundary and durable expiry worker integration."""
import importlib.util
import time
import unittest
from unittest.mock import Mock

AVAILABLE = all(importlib.util.find_spec(name) for name in ('fastapi', 'httpx', 'cryptography'))
if AVAILABLE:
    from fastapi.testclient import TestClient
    from latch_api.device_app import create_app
    from latch_api.device_access import AccessError


@unittest.skipUnless(AVAILABLE, 'Install latch_api/requirements.txt')
class DeviceHTTP(unittest.TestCase):
    token = 'test-token-' + 'x' * 40

    def setUp(self):
        self.access = Mock()
        self.access.issue.return_value = {'grant_id': 'example', 'state': 'active'}
        self.headers = {'Authorization': 'Bearer ' + self.token}
        self.payload = {'device_id': 'freertr', 'role': 'readonly', 'public_key': 'ssh-ed25519 ' + 'A' * 40}

    def client(self, **kwargs):
        return TestClient(create_app(self.access, self.token, **kwargs))

    def test_all_operations_require_authentication_before_coordinator(self):
        with self.client() as client:
            for method, path, data in [('POST', '/v1/device-grants', self.payload),
                                       ('GET', '/v1/device-grants/example', None),
                                       ('DELETE', '/v1/device-grants/example', None)]:
                response = client.request(method, path, json=data)
                self.assertEqual(response.status_code, 401)
            self.assertEqual(client.get('/health/live').status_code, 200)
        self.access.issue.assert_not_called()
        self.access.get.assert_not_called()
        self.access.revoke.assert_not_called()

    def test_identity_comes_from_token_and_body_cannot_override_it(self):
        with self.client() as client:
            response = client.post('/v1/device-grants', headers=self.headers, json=self.payload)
            self.assertEqual(response.status_code, 201)
            self.assertEqual(self.access.issue.call_args.args[0], 'lab-operator')
            response = client.post('/v1/device-grants', headers=self.headers,
                                   json=dict(self.payload, principal='admin'))
            self.assertEqual(response.status_code, 422)

    def test_admin_role_is_validated_and_authorization_denial_preserved(self):
        payload = dict(self.payload, role='admin')
        with self.client() as client:
            response = client.post('/v1/device-grants', headers=self.headers, json=payload)
            self.assertEqual(response.status_code, 201)
            self.assertEqual(self.access.issue.call_args.args[:3], ('lab-operator', 'freertr', 'admin'))
            self.access.issue.side_effect = AccessError('Role not permitted', 403)
            response = client.post('/v1/device-grants', headers=self.headers, json=payload)
            self.assertEqual(response.status_code, 403)
            response = client.post('/v1/device-grants', headers=self.headers,
                                   json=dict(payload, role='superadmin'))
            self.assertEqual(response.status_code, 422)

    def test_coordinator_ownership_denial_is_preserved(self):
        self.access.get.side_effect = AccessError('Grant not found', 404)
        with self.client() as client:
            response = client.get('/v1/device-grants/other', headers=self.headers)
        self.assertEqual(response.status_code, 404)
        self.access.get.assert_called_once_with('lab-operator', 'other')

    def test_worker_reconciles_on_startup_and_retries_after_failure(self):
        self.access.sweep.side_effect = [0, RuntimeError('private-upstream-secret'), 1, 0, 0, 0]
        with self.assertLogs('uvicorn.error', level='ERROR') as logs:
            with self.client(sweep_interval=0.02):
                deadline = time.monotonic() + 1
                while self.access.sweep.call_count < 3 and time.monotonic() < deadline:
                    time.sleep(0.01)
        self.assertGreaterEqual(self.access.sweep.call_count, 3)
        self.assertNotIn('private-upstream-secret', ''.join(logs.output))


if __name__ == '__main__':
    unittest.main()
