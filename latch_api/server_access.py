"""Durable server grants: isolated gateway admission and shared Neutron leases.

Run one process per database. The process-wide lock deliberately serializes
external changes so the last grant cannot remove a concurrent grant's network
path. Resource intent and trusted inventory snapshots survive process restarts.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.error
import uuid

from latch_api.device_access import AccessError, WarpgateGateway, timestamp, validate_public_key

logger = logging.getLogger('uvicorn.error')


def _cleanup_failure(operation, error):
    # Upstream exceptions can contain response bodies or credential material.
    # Expose only a fixed operation label, exception class and HTTP status.
    status = getattr(error, 'code', None)
    if not isinstance(status, int):
        match = re.search(r'HTTP ([0-9]{3})', str(error))
        status = int(match.group(1)) if match else None
    logger.warning('Server cleanup failed: operation=%s exception=%s status=%s',
                   operation, type(error).__name__, status if status is not None else 'unknown')


@dataclass(frozen=True)
class Server:
    server_id: str
    project_id: str
    port_id: str
    floating_ip_id: str
    host: str
    port: int
    host_public_key: str
    internal_ip: str
    security_group_id: str
    login: str = 'ubuntu'

    def __post_init__(self):
        for value in (self.server_id, self.project_id, self.port_id,
                      self.floating_ip_id, self.security_group_id):
            uuid.UUID(value)
        ipaddress.ip_address(self.host)
        ipaddress.ip_address(self.internal_ip)
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError('Invalid server SSH port')
        if self.login != 'ubuntu':
            raise ValueError('The baseline server signing policy permits only ubuntu')
        validate_public_key(self.host_public_key)


class ServerAccess:
    def __init__(self, database_path, inventory, permissions, network, gateway,
                 connection, clock=time.time):
        self.inventory = dict(inventory)
        self.permissions = {caller: frozenset(instances) for caller, instances in permissions.items()}
        self.network, self.gateway, self.clock = network, gateway, clock
        self.connection = dict(connection)
        validate_public_key(self.connection['host_public_key'])
        if not self.connection.get('host') or not 1 <= int(self.connection['port']) <= 65535:
            raise ValueError('Invalid gateway SSH endpoint')
        self.lock = threading.RLock()
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            # Preserve durable leases and their existing gateway resource names
            # across the VM-to-server terminology change.
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='vm_grants'").fetchone():
                db.execute('ALTER TABLE vm_grants RENAME TO server_grants')
                db.execute('ALTER TABLE server_grants RENAME COLUMN instance_id TO server_id')
                db.execute('ALTER TABLE server_grants RENAME COLUMN vm_json TO server_json')
                db.execute('ALTER TABLE server_grants ADD COLUMN gateway_name TEXT')
                for row in db.execute('SELECT id,server_json FROM server_grants').fetchall():
                    snapshot = json.loads(row['server_json'])
                    snapshot['server_id'] = snapshot.pop('instance_id')
                    db.execute('UPDATE server_grants SET server_json=?,gateway_name=? WHERE id=?',
                               (json.dumps(snapshot), 'latch-vm-' + row['id'].replace('-', ''), row['id']))
            db.execute('''CREATE TABLE IF NOT EXISTS server_grants (
                id TEXT PRIMARY KEY, principal TEXT NOT NULL, server_id TEXT NOT NULL,
                server_json TEXT NOT NULL, expires REAL NOT NULL, state TEXT NOT NULL,
                idempotency_key TEXT, payload_hash TEXT NOT NULL,
                gateway_name TEXT NOT NULL,
                cleanup_attempts INTEGER NOT NULL DEFAULT 0,
                UNIQUE(principal, idempotency_key))''')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _name(grant_id):
        return 'latch-server-' + grant_id.replace('-', '')

    def _row(self, grant_id):
        with self._db() as db:
            row = db.execute('SELECT * FROM server_grants WHERE id=?', (grant_id,)).fetchone()
        if row is None:
            raise AccessError('Grant not found', 404)
        return row

    def _state(self, grant_id, state):
        with self._db() as db:
            db.execute('UPDATE server_grants SET state=? WHERE id=?', (state, grant_id))

    def _owned(self, caller, grant_id):
        row = self._row(grant_id)
        if row['principal'] != caller:
            raise AccessError('Grant not found', 404)
        return row

    def _public(self, row):
        name = row['gateway_name']
        return {'grant_id': row['id'], 'server_id': row['server_id'],
                'state': row['state'], 'expires_at': timestamp(row['expires']),
                'gateway_host': self.connection['host'], 'gateway_port': int(self.connection['port']),
                'gateway_host_public_key': self.connection['host_public_key'],
                'ssh_username': name + ':' + name}

    def issue(self, caller, server_id, public_key, ttl_seconds=300, idempotency_key=None):
        # Authorization is evaluated even for idempotent retries after policy changes.
        if server_id not in self.permissions.get(caller, ()) or server_id not in self.inventory:
            raise AccessError('Server access is not authorized', 403)
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
            raise AccessError('TTL must be between 1 and 300 seconds', 422)
        public_key = validate_public_key(public_key)
        if idempotency_key is not None and not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', idempotency_key):
            raise AccessError('Invalid idempotency key', 422)
        digest = hashlib.sha256(json.dumps([server_id, public_key, ttl_seconds]).encode()).hexdigest()
        with self.lock:
            if idempotency_key is not None:
                with self._db() as db:
                    previous = db.execute('SELECT * FROM server_grants WHERE principal=? AND idempotency_key=?',
                                          (caller, idempotency_key)).fetchone()
                if previous is not None:
                    if previous['payload_hash'] != digest:
                        raise AccessError('Idempotency key was used for a different request', 409)
                    return self.get(caller, previous['id'])
            server, grant_id = self.inventory[server_id], str(uuid.uuid4())
            expires = self.clock() + ttl_seconds
            with self._db() as db:
                db.execute('''INSERT INTO server_grants
                    (id,principal,server_id,server_json,expires,state,idempotency_key,payload_hash,gateway_name)
                    VALUES(?,?,?,?,?,'creating',?,?,?)''',
                           (grant_id, caller, server_id, json.dumps(asdict(server)), expires,
                            idempotency_key, digest, self._name(grant_id)))
            try:
                self.network.ensure(server)
                self.gateway.provision(self._name(grant_id), server, public_key, timestamp(expires))
                if self.clock() >= expires:
                    raise AccessError('Grant expired during provisioning')
                self._state(grant_id, 'active')
            except Exception:
                self._cleanup(grant_id)
                raise AccessError('Server access provisioning failed; cleanup scheduled') from None
            return self._public(self._row(grant_id))

    def get(self, caller, grant_id):
        with self.lock:
            row = self._owned(caller, grant_id)
            if row['state'] == 'active' and row['expires'] <= self.clock():
                self._cleanup(grant_id)
                row = self._row(grant_id)
            return self._public(row)

    def revoke(self, caller, grant_id):
        with self.lock:
            self._owned(caller, grant_id)
            self._cleanup(grant_id)
            return self._public(self._row(grant_id))

    def _cleanup(self, grant_id):
        row = self._row(grant_id)
        if row['state'] == 'revoked':
            return
        with self._db() as db:
            db.execute("UPDATE server_grants SET state='revoking',cleanup_attempts=cleanup_attempts+1 WHERE id=?",
                       (grant_id,))
        try:
            # Do not release the network lease until this grant's admission and
            # existing sessions are confirmed closed.
            self.gateway.cleanup(row['gateway_name'])
            with self._db() as db:
                others = db.execute("SELECT 1 FROM server_grants WHERE server_id=? AND id<>? AND state<>'revoked' LIMIT 1",
                                    (row['server_id'], grant_id)).fetchone()
            if others is None:
                self.network.remove(Server(**json.loads(row['server_json'])))
        except Exception as error:
            _cleanup_failure('grant_resources', error)
            self._state(grant_id, 'cleanup_pending')
            return
        self._state(grant_id, 'revoked')

    def sweep(self):
        with self.lock:
            with self._db() as db:
                rows = db.execute("SELECT id FROM server_grants WHERE state NOT IN ('active','revoked') OR (state='active' AND expires<=?)",
                                  (self.clock(),)).fetchall()
            for row in rows:
                self._cleanup(row['id'])
            return len(rows)


class ServerGateway(WarpgateGateway):
    """Certificate-backed SSH targets; signing credentials remain in Warpgate."""
    def __init__(self, api, backend='latch-openbao', mount='ssh-client-signer', role='latch-warpgate'):
        super().__init__(api)
        self.backend, self.mount, self.role = backend, mount, role

    def _delete_or_close(self, method, path):
        # Warpgate's SQLite writes can briefly conflict with active recordings.
        # These operations are idempotent, including an already-removed 404;
        # retry only HTTP 500, never creation of users, roles or credentials.
        for attempt in range(5):
            try:
                return super()._delete_or_close(method, path)
            except (urllib.error.HTTPError, RuntimeError) as error:
                server_error = getattr(error, 'code', None) == 500 or str(error).endswith('HTTP 500')
                if not server_error or attempt == 4:
                    raise
                time.sleep(0.1 * 2 ** attempt)

    def provision(self, name, server, public_key, expires_at):
        if not re.fullmatch(r'latch-server-[0-9a-f]{32}', name):
            raise AccessError('Invalid grant namespace')
        parameters = self.api.request('GET', '/parameters')
        if not parameters.get('recordings_enable') or parameters.get('ssh_host_key_verification') != 'AutoReject':
            raise AccessError('Warpgate must record sessions and reject unknown host keys')
        if any(role.get('is_default') for role in self.api.request('GET', '/roles')):
            raise AccessError('Default Warpgate roles must be disabled')
        backends = [backend for backend in self.api.request('GET', '/secret-backends') if backend['name'] == self.backend]
        if len(backends) != 1:
            raise AccessError('Warpgate SSH signing backend is not configured')
        health = self.api.request('POST', '/secret-backends/' + backends[0]['id'] + '/health')
        if health and health.get('error'):
            raise AccessError('Warpgate SSH signing backend is unavailable')
        kind, key = server.host_public_key.split()[:2]
        pins = [pin for pin in self.api.request('GET', '/ssh/known-hosts')
                if pin['host'] == server.host and pin['port'] == server.port]
        if any(pin['key_type'] != kind or pin['key_base64'] != key for pin in pins):
            raise AccessError('Server host key conflicts with the pinned gateway key')
        if not pins:
            self.api.request('POST', '/ssh/known-hosts', {'host': server.host, 'port': server.port,
                             'key_type': kind, 'key_base64': key})
        role = self.api.request('POST', '/roles', {'name': name, 'is_default': False})
        user = self.api.request('POST', '/users', {'username': name})
        self.api.request('POST', '/users/' + user['id'] + '/credentials/public-keys',
                         {'label': 'latch-server-key', 'openssh_public_key': public_key})
        target = self.api.request('POST', '/targets', {
            'name': name, 'description': 'Temporary Latch server grant', 'require_approval': False,
            'ticket_requests_disabled': True, 'ticket_require_approval': False,
            'options': {'kind': 'Ssh', 'host': server.host, 'port': server.port,
                        'username': server.login, 'allow_insecure_algos': False,
                        'auth': {'kind': 'Certificate', 'backend': self.backend,
                                 'mount': self.mount, 'role': self.role}}})
        self.api.request('POST', '/targets/' + target['id'] + '/roles/' + role['id'])
        self.api.request('POST', '/users/' + user['id'] + '/roles/' + role['id'], {'expires_at': expires_at})

    def cleanup(self, name):
        if not re.fullmatch(r'latch-(?:server|vm)-[0-9a-f]{32}', name):
            raise AccessError('Invalid grant namespace')
        failed = False
        # Close admission before enumerating sessions. Otherwise a login could
        # begin between the session snapshot and deletion of its target. Keep
        # the user until after enumeration so the username filter still works.
        try:
            targets = self.api.request('GET', '/targets')
            for target in targets:
                if target['name'] != name:
                    continue
                path = '/targets/' + target['id']
                for role in self.api.request('GET', path + '/roles'):
                    self._delete_or_close('DELETE', path + '/roles/' + role['id'])
                options = target['options']
                endpoint = (options['host'], options['port'])
                peers = [other for other in targets if other['id'] != target['id']
                         and other.get('options', {}).get('kind') == 'Ssh'
                         and (other['options']['host'], other['options']['port']) == endpoint]
                # Warpgate DELETE target also removes the endpoint-wide host
                # pin. Keep disabled targets until no peer has admission, then
                # collect those tombstones together with the final target.
                if any(not re.fullmatch(r'latch-(?:server|vm)-[0-9a-f]{32}', peer['name'])
                       or self.api.request('GET', '/targets/' + peer['id'] + '/roles') for peer in peers):
                    continue
                for retired in [target, *peers]:
                    if re.fullmatch(r'latch-(?:server|vm)-[0-9a-f]{32}', retired['name']):
                        self._delete_or_close('DELETE', '/targets/' + retired['id'])
        except Exception as error:
            _cleanup_failure('target_admission', error)
            failed = True
        try:
            sessions, offset = [], 0
            while True:
                page = self.api.request('GET', '/sessions?' + urllib.parse.urlencode(
                    {'username': name, 'active_only': 'true', 'limit': 100, 'offset': offset}))
                sessions.extend(page['items'])
                if len(page['items']) < 100:
                    break
                offset += len(page['items'])
            for session in sessions:
                self._delete_or_close('POST', '/sessions/' + session['id'] + '/close')
        except Exception as error:
            _cleanup_failure('active_sessions', error)
            failed = True
        if failed:
            # A retry must still be able to resolve the grant's username when
            # enumerating active sessions. Deleting its user now can hide an
            # unclosed session from that filter on a later reconciliation.
            raise AccessError('Warpgate cleanup incomplete')
        for collection, key, endpoint in (('/users', 'username', '/users/'),
                                          ('/roles', 'name', '/role/')):
            try:
                for obj in self.api.request('GET', collection):
                    if obj[key] == name:
                        self._delete_or_close('DELETE', endpoint + obj['id'])
            except Exception as error:
                _cleanup_failure('user' if collection == '/users' else 'role', error)
                failed = True
        if failed:
            raise AccessError('Warpgate cleanup incomplete')
