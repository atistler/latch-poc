"""Device authorization, compensation and durable expiry with isolated service doubles."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from latch_api.device_access import AccessError, Device, DeviceAccess, LDAPLease, OpenBaoLDAP, WarpgateGateway


def key():
    return Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()


class Bao:
    def __init__(self):
        self.leases, self.revoked, self.fail_revoke = [], set(), False
        self.ttl = 300

    def issue(self, role):
        lease = LDAPLease('ldap/creds/lease-' + str(len(self.leases)),
                          'temporary-' + str(len(self.leases)), 'secret-password', self.ttl)
        self.leases.append(lease)
        return lease

    def revoke(self, lease_id):
        if self.fail_revoke:
            raise RuntimeError('secret-password in upstream exception')
        self.revoked.add(lease_id)


class Gateway:
    def __init__(self):
        self.grants, self.cleaned = {}, []
        self.fail_provision = self.fail_cleanup = False

    def provision(self, name, device, lease, public_key, expires_at):
        self.grants[name] = (device, lease, public_key, expires_at)
        if self.fail_provision:
            raise RuntimeError('secret-password in upstream exception')

    def cleanup(self, name):
        if self.fail_cleanup:
            raise RuntimeError('secret-password in upstream exception')
        self.cleaned.append(name)
        self.grants.pop(name, None)


class DeviceGrants(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'grants.db'
        self.now = 1000
        self.public = key()
        self.device = Device('10.1.3.22', 22, key())
        self.bao, self.gateway = Bao(), Gateway()
        self.service = self.new_service()

    def new_service(self):
        return DeviceAccess(self.db, {'freertr': self.device},
                            {'alice': {'freertr': ['readonly']}, 'bob': {'freertr': ['operator']}},
                            self.bao, self.gateway, clock=lambda: self.now)

    def issue(self, **kwargs):
        return self.service.issue('alice', 'freertr', 'readonly', self.public, **kwargs)

    def test_unauthorized_device_role_and_principal_never_issue_credentials(self):
        for principal, device, role in [('mallory', 'freertr', 'readonly'), ('alice', 'other', 'readonly'),
                                         ('alice', 'freertr', 'operator'), ('alice', 'freertr', 'admin')]:
            with self.subTest(principal=principal, device=device, role=role):
                with self.assertRaises(AccessError) as error:
                    self.service.issue(principal, device, role, self.public)
                self.assertEqual(error.exception.status_code, 403)
        self.assertEqual(self.bao.leases, [])

    def test_explicit_admin_permission_and_device_role_are_both_required(self):
        inventory = {'freertr': Device('10.1.3.22', 22, key(), roles=['readonly', 'operator', 'admin'])}
        permissions = {'alice': {'freertr': ['admin']}}
        service = DeviceAccess(self.db, inventory, permissions, self.bao, self.gateway, clock=lambda: self.now)
        with patch.object(self.bao, 'issue', wraps=self.bao.issue) as issue:
            grant = service.issue('alice', 'freertr', 'admin', self.public)
        self.assertEqual(grant['role'], 'admin')
        issue.assert_called_once_with('admin')
        restricted = DeviceAccess(self.db, {'freertr': self.device}, permissions, self.bao, self.gateway)
        with self.assertRaises(AccessError) as error:
            restricted.issue('alice', 'freertr', 'admin', self.public)
        self.assertEqual(error.exception.status_code, 403)
        self.assertEqual(len(self.bao.leases), 1)

    def test_bad_key_or_unbounded_ttl_rejected_before_credentials(self):
        for invalid in ('not a key', self.public + '\n' + self.public, ''):
            with self.assertRaises(AccessError):
                self.service.issue('alice', 'freertr', 'readonly', invalid)
        for ttl in (0, 301, True, '300'):
            with self.assertRaises(AccessError):
                self.issue(ttl_seconds=ttl)
        self.assertEqual(self.bao.leases, [])

    def test_unique_concurrent_grants_and_no_persisted_password(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            grants = list(pool.map(lambda _: self.issue(), range(6)))
        self.assertEqual(len({grant['ssh_username'] for grant in grants}), 6)
        self.assertEqual(len({g[1].username for g in self.gateway.grants.values()}), 6)
        self.assertNotIn('secret-password', self.db.read_bytes().decode(errors='ignore'))
        self.assertNotIn('lease-', json.dumps(grants))
        self.assertNotIn('secret-password', repr(self.bao.leases))
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)

    def test_restarted_service_expires_and_revokes_grant(self):
        grant = self.issue(ttl_seconds=10)
        self.now += 11
        restarted = self.new_service()
        self.assertEqual(restarted.sweep(), 1)
        self.assertEqual(restarted.get('alice', grant['grant_id'])['state'], 'revoked')
        self.assertFalse(self.gateway.grants)
        self.assertEqual(self.bao.revoked, {self.bao.leases[0].lease_id})
        self.assertEqual(restarted.sweep(), 0)

    def test_owner_required_and_parallel_revoke_is_idempotent(self):
        grant = self.issue()
        with self.assertRaises(AccessError) as error:
            self.service.revoke('bob', grant['grant_id'])
        self.assertEqual(error.exception.status_code, 404)
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda _: self.service.revoke('alice', grant['grant_id']), range(5)))
        self.assertTrue(all(r['state'] == 'revoked' for r in results))
        self.assertEqual(len(self.gateway.cleaned), 1)

    def test_partial_creation_compensated_and_secret_errors_redacted(self):
        self.gateway.fail_provision = True
        with self.assertRaises(AccessError) as error:
            self.issue()
        self.assertNotIn('secret-password', str(error.exception))
        self.assertFalse(self.gateway.grants)
        self.assertEqual(len(self.bao.revoked), 1)

    def test_cleanup_outage_persisted_and_retried_after_restart(self):
        grant = self.issue()
        self.gateway.fail_cleanup = True
        self.bao.fail_revoke = True
        self.assertEqual(self.service.revoke('alice', grant['grant_id'])['state'], 'cleanup_pending')
        self.gateway.fail_cleanup = False
        self.bao.fail_revoke = False
        restarted = self.new_service()
        restarted.sweep()
        self.assertEqual(restarted.get('alice', grant['grant_id'])['state'], 'revoked')
        self.assertFalse(self.gateway.grants)

    def test_partial_creation_and_cleanup_failure_is_durable(self):
        self.gateway.fail_provision = self.gateway.fail_cleanup = True
        with self.assertRaises(AccessError):
            self.issue()
        self.assertEqual(len(self.bao.revoked), 1)  # LDAP revoked even if gateway is down.
        self.gateway.fail_cleanup = False
        self.new_service().sweep()
        self.assertFalse(self.gateway.grants)

    def test_slow_new_grant_does_not_block_expired_grant_cleanup(self):
        grant = self.issue(ttl_seconds=1)
        original = self.gateway.provision
        started, release = threading.Event(), threading.Event()
        def slow(*args):
            started.set()
            if not release.wait(5):
                raise RuntimeError('Test synchronization timeout')
            original(*args)
        self.gateway.provision = slow
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(self.issue)
            try:
                self.assertTrue(started.wait(2))
                self.now += 2
                sweep = pool.submit(self.service.sweep)
                self.assertEqual(sweep.result(timeout=2), 1)
                self.assertEqual(self.service.get('alice', grant['grant_id'])['state'], 'revoked')
                self.assertEqual(len(self.bao.revoked), 1)
            finally:
                release.set()
            new_grant = pending.result(timeout=2)
        self.assertEqual(new_grant['state'], 'active')
        self.assertEqual(len(self.gateway.grants), 1)

    def test_engine_cannot_exceed_five_minute_lease_limit(self):
        self.bao.ttl = 600
        with self.assertRaises(AccessError):
            self.issue()
        self.assertEqual(len(self.bao.revoked), 1)
        self.assertFalse(self.gateway.grants)

    def test_expiry_during_provisioning_is_compensated(self):
        original = self.gateway.provision
        def slow(*args):
            original(*args)
            self.now += 301
        self.gateway.provision = slow
        with self.assertRaises(AccessError):
            self.issue()
        self.assertFalse(self.gateway.grants)


class GatewayContract(unittest.TestCase):
    def test_password_auth_role_expiry_and_cleanup_exact_namespace(self):
        class API:
            def __init__(self):
                self.calls = []
                self.objects = {'/users': [], '/targets': [], '/roles': []}
            def request(self, method, path, data=None):
                self.calls.append((method, path, data))
                if method == 'GET':
                    if path == '/parameters':
                        return {'recordings_enable': True, 'ssh_host_key_verification': 'AutoReject'}
                    if path.startswith('/sessions?'):
                        return {'items': [{'id': 'session'}]}
                    return self.objects.get(path, [])
                if method == 'POST' and path in self.objects:
                    result = dict(data, id=path[1:] + '-id')
                    self.objects[path].append(result)
                    return result
        api = API()
        gateway = WarpgateGateway(api)
        name = 'latch-device-' + 'a' * 32
        gateway.provision(name, Device('10.1.3.22', 22, key()),
                          LDAPLease('lease-id', 'ldap-user', 'temporary-password', 300), key(),
                          '2026-10-30T12:00:00Z')
        target = next(data for method, path, data in api.calls if method == 'POST' and path == '/targets')
        self.assertEqual(target['options']['auth'], {'kind': 'Password', 'password': 'temporary-password'})
        self.assertIn(('POST', '/users/users-id/roles/roles-id',
                       {'expires_at': '2026-10-30T12:00:00Z'}), api.calls)
        gateway.cleanup(name)
        paths = [(method, path) for method, path, data in api.calls]
        self.assertLess(paths.index(('POST', '/sessions/session/close')), paths.index(('DELETE', '/targets/targets-id')))
        self.assertIn(('DELETE', '/role/roles-id'), paths)
        with self.assertRaises(AccessError):
            gateway.cleanup('latch-vm')

    def test_host_key_conflict_fails_before_creating_user(self):
        class API:
            def request(self, method, path, data=None):
                if path == '/parameters':
                    return {'recordings_enable': True, 'ssh_host_key_verification': 'AutoReject'}
                if path == '/roles':
                    return []
                if path != '/ssh/known-hosts':
                    raise AssertionError('Unexpected mutation')
                return [{'host': '10.1.3.22', 'port': 22, 'key_type': 'ssh-ed25519', 'key_base64': 'wrong'}]
        with self.assertRaises(AccessError):
            WarpgateGateway(API()).provision('name', Device('10.1.3.22', 22, key()),
                                             LDAPLease('lease', 'user', 'secret', 300), key(), 'expiry')

    def test_openbao_uses_isolated_role_endpoint_and_redacts_transport_failure(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self, limit):
                return json.dumps({'lease_id': 'lease', 'lease_duration': 300,
                                   'data': {'username': 'temporary', 'password': 'secret-password'}}).encode()
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / 'token'
            token.write_text('secret-token')
            token.chmod(0o600)
            bao = OpenBaoLDAP('http://ldap-openbao:8200', token)
            with patch('urllib.request.build_opener') as build:
                build.return_value.open.return_value = Response()
                lease = bao.issue('admin')
                request = build.return_value.open.call_args.args[0]
                self.assertEqual(request.full_url, 'http://ldap-openbao:8200/v1/ldap/creds/admin')
                self.assertEqual(request.get_header('X-vault-token'), 'secret-token')
                self.assertEqual(lease.username, 'temporary')
                build.return_value.open.side_effect = TimeoutError('secret-password')
                with self.assertRaises(AccessError) as error:
                    bao.issue('operator')
                self.assertNotIn('secret-password', str(error.exception))

    def test_gateway_requires_recording_strict_host_keys_and_no_default_roles(self):
        class API:
            def __init__(self, parameters, roles):
                self.parameters, self.roles = parameters, roles
            def request(self, method, path, data=None):
                if path == '/parameters':
                    return self.parameters
                if path == '/roles' and method == 'GET':
                    return self.roles
                raise AssertionError('No mutation allowed')
        for parameters, roles in [({'recordings_enable': False}, []),
                                  ({'recordings_enable': True, 'ssh_host_key_verification': 'AutoAccept'}, []),
                                  ({'recordings_enable': True, 'ssh_host_key_verification': 'AutoReject'},
                                   [{'is_default': True}])]:
            with self.assertRaises(AccessError):
                WarpgateGateway(API(parameters, roles)).provision('name', Device('10.1.3.22', 22, key()),
                    LDAPLease('lease', 'user', 'secret', 300), key(), 'expiry')

    def test_token_file_world_permissions_rejected_before_http(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / 'token'
            token.write_text('secret-token')
            token.chmod(0o644)
            with patch('urllib.request.build_opener') as request:
                with self.assertRaises(AccessError):
                    OpenBaoLDAP('http://openbao:8200', token).issue('readonly')
                request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
