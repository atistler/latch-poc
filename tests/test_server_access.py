"""Server access lifetime, shared path ownership and failure recovery."""
import importlib.util
import json
from dataclasses import asdict
from pathlib import Path
import tempfile
import sqlite3
import threading
import unittest
from unittest.mock import Mock, patch

AVAILABLE = importlib.util.find_spec('cryptography') is not None
if AVAILABLE:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from latch_api.device_access import AccessError
    from latch_api.server_access import Server, ServerAccess, ServerGateway


@unittest.skipUnless(AVAILABLE, 'Install latch_api/requirements.txt')
class ServerGrants(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'grants.sqlite'
        self.key = Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
        self.server = Server('11111111-1111-1111-1111-111111111111',
                     '22222222-2222-2222-2222-222222222222',
                     '33333333-3333-3333-3333-333333333333',
                     '44444444-4444-4444-4444-444444444444', '100.64.10.120', 22001,
                     self.key, '192.168.20.15', '55555555-5555-5555-5555-555555555555')
        self.now = 1000
        self.network, self.gateway = Mock(), Mock()
        self.access = self.coordinator()

    def coordinator(self, inventory=None):
        return ServerAccess(self.path, inventory or {self.server.server_id: self.server},
                        {'alice': [self.server.server_id], 'bob': [self.server.server_id]},
                        self.network, self.gateway,
                        {'host': '127.0.0.1', 'port': 9202, 'host_public_key': self.key},
                        clock=lambda: self.now)

    def issue(self, caller='alice', **kwargs):
        return self.access.issue(caller, self.server.server_id, self.key, **kwargs)

    def test_distinct_grants_share_path_and_revoke_independently(self):
        first, second = self.issue(), self.issue('bob')
        self.assertNotEqual(first['ssh_username'], second['ssh_username'])
        self.assertEqual(first['state'], 'active')
        self.assertEqual(self.access.revoke('alice', first['grant_id'])['state'], 'revoked')
        self.network.remove.assert_not_called()
        self.assertEqual(self.access.get('bob', second['grant_id'])['state'], 'active')
        self.access.revoke('bob', second['grant_id'])
        self.network.remove.assert_called_once_with(self.server)
        self.access.revoke('bob', second['grant_id'])
        self.assertEqual(self.network.remove.call_count, 1)

    def test_legacy_database_preserves_grant_admission_and_cleanup(self):
        grant_id = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
        snapshot = asdict(self.server)
        snapshot['instance_id'] = snapshot.pop('server_id')
        self.path.unlink()
        with sqlite3.connect(self.path) as db:
            db.execute('''CREATE TABLE vm_grants (
                id TEXT PRIMARY KEY, principal TEXT NOT NULL, instance_id TEXT NOT NULL,
                vm_json TEXT NOT NULL, expires REAL NOT NULL, state TEXT NOT NULL,
                idempotency_key TEXT, payload_hash TEXT NOT NULL,
                cleanup_attempts INTEGER NOT NULL DEFAULT 0,
                UNIQUE(principal,idempotency_key))''')
            db.execute('INSERT INTO vm_grants VALUES (?,?,?,?,?,?,?,?,?)',
                       (grant_id, 'alice', self.server.server_id, json.dumps(snapshot),
                        self.now + 100, 'active', None, 'old-digest', 0))
        access = self.coordinator()
        old_name = 'latch-vm-' + grant_id.replace('-', '')
        grant = access.get('alice', grant_id)
        self.assertEqual(grant['ssh_username'], old_name + ':' + old_name)
        self.assertEqual(grant['server_id'], self.server.server_id)
        self.assertEqual(access.revoke('alice', grant_id)['state'], 'revoked')
        self.gateway.cleanup.assert_called_once_with(old_name)
        self.network.remove.assert_called_once_with(self.server)
        # Startup migration is idempotent; later grants use the new namespace.
        self.access = self.coordinator()
        self.assertTrue(self.issue()['ssh_username'].startswith('latch-server-'))

    def test_owner_and_inventory_denials_have_no_side_effects(self):
        for caller, instance in [('mallory', self.server.server_id), ('alice', 'unknown')]:
            with self.assertRaises(AccessError) as error:
                self.access.issue(caller, instance, self.key)
            self.assertEqual(error.exception.status_code, 403)
        self.network.ensure.assert_not_called()
        grant = self.issue()
        for operation in (self.access.get, self.access.revoke):
            with self.assertRaises(AccessError) as error:
                operation('bob', grant['grant_id'])
            self.assertEqual(error.exception.status_code, 404)

    def test_ttl_and_key_validation_precede_external_changes(self):
        for ttl in (0, 301, True, '100'):
            with self.assertRaises(AccessError):
                self.issue(ttl_seconds=ttl)
        with self.assertRaises(AccessError):
            self.access.issue('alice', self.server.server_id, 'invalid')
        self.network.ensure.assert_not_called()

    def test_idempotency_is_durable_payload_checked_and_caller_scoped(self):
        first = self.issue(idempotency_key='retry-1')
        self.assertEqual(first, self.issue(idempotency_key='retry-1'))
        self.assertEqual(self.network.ensure.call_count, 1)
        self.access = self.coordinator()
        self.assertEqual(first, self.issue(idempotency_key='retry-1'))
        with self.assertRaises(AccessError) as error:
            self.issue(idempotency_key='retry-1', ttl_seconds=100)
        self.assertEqual(error.exception.status_code, 409)
        self.assertNotEqual(first['grant_id'], self.issue('bob', idempotency_key='retry-1')['grant_id'])

    def test_expiry_closes_admission_before_network_and_restart_reconciles(self):
        events = []
        grant = self.issue(ttl_seconds=10)
        self.gateway.cleanup.side_effect = lambda name: events.append('gateway')
        self.network.remove.side_effect = lambda server: events.append('network')
        self.now += 11
        self.access = self.coordinator()
        self.assertEqual(self.access.sweep(), 1)
        self.assertEqual(events, ['gateway', 'network'])
        self.assertEqual(self.access.get('alice', grant['grant_id'])['state'], 'revoked')

    def test_gateway_failure_preserves_network_lease_and_reports_pending(self):
        grant = self.issue()
        self.gateway.cleanup.side_effect = RuntimeError('private-upstream-secret')
        self.assertEqual(self.access.revoke('alice', grant['grant_id'])['state'], 'cleanup_pending')
        self.network.remove.assert_not_called()
        self.gateway.cleanup.side_effect = None
        self.access.sweep()
        self.network.remove.assert_called_once_with(self.server)
        self.assertEqual(self.access.get('alice', grant['grant_id'])['state'], 'revoked')

    def test_network_cleanup_failure_retries_using_persisted_inventory(self):
        grant = self.issue()
        self.network.remove.side_effect = RuntimeError('offline')
        self.assertEqual(self.access.revoke('alice', grant['grant_id'])['state'], 'cleanup_pending')
        self.access.inventory.clear()
        self.network.remove.side_effect = None
        self.access.sweep()
        self.assertEqual(self.access.get('alice', grant['grant_id'])['state'], 'revoked')
        self.network.remove.assert_called_with(self.server)

    def test_provisioning_failure_compensates_without_leaking_error(self):
        self.gateway.provision.side_effect = RuntimeError('signer-secret')
        with self.assertRaises(AccessError) as error:
            self.issue()
        self.assertNotIn('signer-secret', str(error.exception))
        self.gateway.cleanup.assert_called_once()
        self.network.remove.assert_called_once_with(self.server)
        self.assertEqual(self.access.sweep(), 0)

    def test_interrupted_creation_is_recovered_on_restart(self):
        grant = self.issue()
        self.access._state(grant['grant_id'], 'creating')
        self.access = self.coordinator()
        self.access.sweep()
        self.assertEqual(self.access.get('alice', grant['grant_id'])['state'], 'revoked')

    def test_concurrent_issue_waits_for_last_grant_teardown(self):
        first = self.issue()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        self.network.remove.side_effect = lambda server: (entered.set(), release.wait(2))
        revoke = threading.Thread(target=lambda: self.access.revoke('alice', first['grant_id']))
        revoke.start()
        self.assertTrue(entered.wait(1))
        created = []
        issue = threading.Thread(target=lambda: (created.append(self.issue('bob')), finished.set()))
        issue.start()
        self.assertFalse(finished.wait(0.05))
        release.set()
        revoke.join(2)
        issue.join(2)
        self.assertTrue(finished.is_set())
        self.assertEqual(created[0]['state'], 'active')

    def test_database_permissions_and_public_response_exclude_credentials(self):
        response = self.issue()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(response), {'grant_id', 'server_id', 'state', 'expires_at',
                                        'gateway_host', 'gateway_port', 'gateway_host_public_key', 'ssh_username'})


@unittest.skipUnless(AVAILABLE, 'Install latch_api/requirements.txt')
class ServerGatewayTests(unittest.TestCase):
    setUp = ServerGrants.setUp
    coordinator = ServerGrants.coordinator

    def fake_api(self, pin=None):
        api = Mock()
        calls = []
        def request(method, path, data=None):
            calls.append((method, path, data))
            if path == '/parameters':
                return {'recordings_enable': True, 'ssh_host_key_verification': 'AutoReject'}
            if path == '/roles' and method == 'GET':
                return []
            if path == '/secret-backends':
                return [{'name': 'latch-openbao', 'id': 'backend'}]
            if path == '/ssh/known-hosts' and method == 'GET':
                return [] if pin is None else [pin]
            return {'id': 'id'}
        api.request.side_effect = request
        return api, calls

    def test_gateway_configures_certificate_auth_and_expiring_role(self):
        api, calls = self.fake_api()
        name = 'latch-server-' + 'a' * 32
        ServerGateway(api).provision(name, self.server, self.key, '2030-01-01T00:00:00Z')
        target = next(data for method, path, data in calls if method == 'POST' and path == '/targets')
        self.assertEqual(target['options']['auth'], {'kind': 'Certificate', 'backend': 'latch-openbao',
                                                   'mount': 'ssh-client-signer', 'role': 'latch-warpgate'})
        self.assertNotIn('password', json.dumps(target).lower())
        assignment = next(data for method, path, data in calls if '/users/id/roles/id' == path)
        self.assertEqual(assignment, {'expires_at': '2030-01-01T00:00:00Z'})

    def test_changed_pin_is_rejected_before_creating_user_or_target(self):
        api, calls = self.fake_api({'host': self.server.host, 'port': self.server.port,
                                    'key_type': 'ssh-ed25519', 'key_base64': 'wrong'})
        with self.assertRaises(AccessError):
            ServerGateway(api).provision('latch-server-' + 'a' * 32, self.server, self.key, '2030-01-01T00:00:00Z')
        self.assertFalse(any(method == 'POST' and path in ('/roles', '/users', '/targets')
                             for method, path, data in calls))

    def test_cleanup_rejects_other_namespaces(self):
        api = Mock()
        with self.assertRaises(AccessError):
            ServerGateway(api).cleanup('latch-device-' + 'a' * 32)
        api.request.assert_not_called()

    def test_cleanup_only_removes_exact_grant_and_closes_its_sessions(self):
        name = 'latch-server-' + 'a' * 32
        api, calls = Mock(), []
        def request(method, path, data=None):
            calls.append((method, path))
            if path.startswith('/sessions?'):
                self.assertIn('username=' + name, path)
                return {'items': [{'id': 'owned-session'}]}
            if path == '/targets':
                return [{'id': 'owned-target', 'name': name, 'options': {'kind': 'Ssh', 'host': self.server.host, 'port': self.server.port}},
                        {'id': 'other-target', 'name': 'unrelated', 'options': {'kind': 'Ssh', 'host': 'other', 'port': 22}}]
            if path == '/users':
                return [{'id': 'owned-user', 'username': name}, {'id': 'other-user', 'username': 'admin'}]
            if path == '/roles':
                return [{'id': 'owned-role', 'name': name}, {'id': 'other-role', 'name': 'unrelated'}]
            return {}
        api.request.side_effect = request
        ServerGateway(api).cleanup(name)
        self.assertIn(('POST', '/sessions/owned-session/close'), calls)
        scan = next(index for index, (_, path) in enumerate(calls) if path.startswith('/sessions?'))
        self.assertLess(calls.index(('DELETE', '/targets/owned-target')), scan)
        self.assertLess(calls.index(('POST', '/sessions/owned-session/close')),
                        calls.index(('DELETE', '/users/owned-user')))
        self.assertEqual([path for method, path in calls if method == 'DELETE'],
                         ['/targets/owned-target', '/users/owned-user', '/role/owned-role'])

    def test_admission_removal_failure_still_closes_sessions_but_remains_pending(self):
        name = 'latch-server-' + 'b' * 32
        api, calls = Mock(), []
        def request(method, path, data=None):
            calls.append((method, path))
            if path == '/targets':
                return [{'id': 'target', 'name': name, 'options': {'kind': 'Ssh', 'host': self.server.host, 'port': self.server.port}}]
            if path == '/targets/target':
                raise RuntimeError('unavailable')
            if path.startswith('/sessions?'):
                return {'items': [{'id': 'session'}]}
            if path in ('/users', '/roles'):
                return []
            return {}
        api.request.side_effect = request
        with self.assertRaises(AccessError):
            ServerGateway(api).cleanup(name)
        self.assertIn(('POST', '/sessions/session/close'), calls)

    def test_failed_session_close_keeps_user_for_username_retry(self):
        name = 'latch-server-' + 'c' * 32
        api, calls = Mock(), []
        unavailable = True
        def request(method, path, data=None):
            calls.append((method, path))
            if path == '/targets':
                return []
            if path.startswith('/sessions?'):
                return {'items': [{'id': 'session'}]}
            if path == '/sessions/session/close' and unavailable:
                raise RuntimeError('upstream unavailable')
            if path == '/users':
                return [{'id': 'user', 'username': name}]
            if path == '/roles':
                return [{'id': 'role', 'name': name}]
            return {}
        api.request.side_effect = request
        gateway = ServerGateway(api)
        with self.assertRaises(AccessError):
            gateway.cleanup(name)
        self.assertNotIn(('DELETE', '/users/user'), calls)
        self.assertNotIn(('DELETE', '/role/role'), calls)
        unavailable = False
        gateway.cleanup(name)
        self.assertIn(('DELETE', '/users/user'), calls)
        self.assertIn(('DELETE', '/role/role'), calls)

    def test_idempotent_cleanup_retries_transient_gateway_database_conflicts(self):
        api = Mock()
        api.request.side_effect = [RuntimeError('Warpgate DELETE /targets/id: HTTP 500'), None]
        with patch('latch_api.server_access.time.sleep') as sleep:
            ServerGateway(api)._delete_or_close('DELETE', '/targets/id')
        self.assertEqual(api.request.call_count, 2)
        sleep.assert_called_once_with(0.1)

    def test_cleanup_retry_is_bounded_and_does_not_retry_permission_errors(self):
        api = Mock()
        for status, attempts in ((500, 5), (403, 1)):
            with self.subTest(status=status):
                api.reset_mock()
                api.request.side_effect = RuntimeError('Warpgate DELETE /targets/id: HTTP ' + str(status))
                with patch('latch_api.server_access.time.sleep'), self.assertRaises(RuntimeError):
                    ServerGateway(api)._delete_or_close('DELETE', '/targets/id')
                self.assertEqual(api.request.call_count, attempts)

    def test_cleanup_logs_never_expose_upstream_error_body(self):
        api = Mock()
        api.request.side_effect = RuntimeError('PRIVATE-CREDENTIAL server body HTTP 403')
        with self.assertLogs('uvicorn.error', level='WARNING') as logs:
            with self.assertRaises(AccessError):
                ServerGateway(api).cleanup('latch-server-' + 'd' * 32)
        self.assertNotIn('PRIVATE-CREDENTIAL', '\n'.join(logs.output))
        self.assertIn('exception=RuntimeError status=403', '\n'.join(logs.output))

    def test_shared_endpoint_pin_survives_until_last_target_loses_admission(self):
        first, second = 'latch-vm-' + 'a' * 32, 'latch-server-' + 'b' * 32
        targets = [{'id': name, 'name': name,
                    'options': {'kind': 'Ssh', 'host': self.server.host, 'port': self.server.port}}
                   for name in (first, second)]
        assignments = {first: [{'id': 'first-role'}], second: [{'id': 'second-role'}]}
        calls, pin = [], [True]
        def request(method, path, data=None):
            calls.append((method, path))
            if path == '/targets':
                return list(targets)
            if path.startswith('/targets/'):
                identifier = path.split('/')[2]
                if method == 'GET':
                    return assignments[identifier]
                if '/roles/' in path:
                    assignments[identifier] = []
                else:
                    targets[:] = [target for target in targets if target['id'] != identifier]
                    pin[0] = False  # Actual Warpgate endpoint-wide side effect.
                return None
            if path.startswith('/sessions?'):
                return {'items': []}
            if path in ('/users', '/roles'):
                return []
            return {}
        api = Mock()
        api.request.side_effect = request
        gateway = ServerGateway(api)
        gateway.cleanup(first)
        self.assertTrue(pin[0])
        self.assertEqual(len(targets), 2)
        self.assertEqual(assignments[first], [])
        self.assertEqual(assignments[second], [{'id': 'second-role'}])
        gateway.cleanup(second)
        self.assertFalse(pin[0])
        self.assertEqual(targets, [])

    def test_foreign_target_on_shared_endpoint_preserves_pin_and_is_never_deleted(self):
        name = 'latch-server-' + 'e' * 32
        api, deleted = Mock(), []
        def request(method, path, data=None):
            if path == '/targets':
                return [{'id': identifier, 'name': identifier,
                         'options': {'kind': 'Ssh', 'host': self.server.host, 'port': self.server.port}}
                        for identifier in (name, 'operator-maintained')]
            if path == '/targets/operator-maintained/roles':
                return []  # Even an inactive foreign reference retains its pin.
            if path.endswith('/roles') or path in ('/users', '/roles'):
                return []
            if path.startswith('/sessions?'):
                return {'items': []}
            if method == 'DELETE':
                deleted.append(path)
            return None
        api.request.side_effect = request
        ServerGateway(api).cleanup(name)
        self.assertEqual(deleted, [])


if __name__ == '__main__':
    unittest.main()
