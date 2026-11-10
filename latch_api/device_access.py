"""Short-lived, isolated device SSH grants; caller identity is supplied by the API.

The API adapter must authenticate before calling this module. Run one service
process per SQLite database and call sweep at startup and periodically. Passwords
are absent from this service's database but are held in Warpgate's persistent
database during each grant. Database journals/backups may retain revoked values.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import ssl
import stat
import threading
import time
from types import MappingProxyType
import urllib.error
import urllib.parse
import urllib.request
import uuid

from cryptography.hazmat.primitives import serialization


class AccessError(Exception):
    def __init__(self, message, status_code=503):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class Device:
    host: str
    port: int
    host_public_key: str
    roles: tuple = ('readonly', 'operator')

    def __post_init__(self):
        if not self.host or any(c.isspace() for c in self.host) or not 1 <= self.port <= 65535:
            raise ValueError('Invalid operator device endpoint')
        validate_public_key(self.host_public_key)
        if not set(self.roles) <= {'readonly', 'operator', 'admin'}:
            raise ValueError('Unsupported device role')
        object.__setattr__(self, 'roles', tuple(self.roles))


@dataclass(frozen=True)
class LDAPLease:
    lease_id: str = field(repr=False)
    username: str
    password: str = field(repr=False)
    ttl_seconds: int


def validate_public_key(value):
    if not isinstance(value, str) or len(value) > 16384 or '\n' in value or '\r' in value:
        raise AccessError('A single OpenSSH public key is required', 422)
    try:
        serialization.load_ssh_public_key(value.encode())
    except Exception:
        # cryptography also raises UnsupportedAlgorithm for unsupported key types.
        raise AccessError('Invalid OpenSSH public key', 422) from None
    return value.strip()


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace('+00:00', 'Z')


class DeviceAccess:
    """Durable grant coordinator. Dependencies must raise on incomplete operations."""
    def __init__(self, database_path, inventory, permissions, bao, gateway, clock=time.time):
        self.inventory = MappingProxyType(dict(inventory))
        self.permissions = {p: {d: frozenset(r) for d, r in ds.items()} for p, ds in permissions.items()}
        self.bao, self.gateway, self.clock = bao, gateway, clock
        self.lock = threading.Lock()
        self.grant_locks = {}
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Avoid creating a world-readable journal/database even under a permissive umask.
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        with self._db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS grants (
                id TEXT PRIMARY KEY, principal TEXT NOT NULL, device_id TEXT NOT NULL,
                role TEXT NOT NULL, expires REAL NOT NULL, state TEXT NOT NULL,
                lease_id TEXT, cleanup_attempts INTEGER NOT NULL DEFAULT 0)''')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _grant_lock(self, grant_id):
        # Only map access is global; network calls never hold the map lock.
        with self.lock:
            return self.grant_locks.setdefault(grant_id, threading.RLock())

    def _row(self, grant_id):
        with self._db() as db:
            row = db.execute('SELECT * FROM grants WHERE id=?', (grant_id,)).fetchone()
        if row is None:
            raise AccessError('Grant not found', 404)
        return row

    def _update(self, grant_id, **values):
        with self._db() as db:
            db.execute('UPDATE grants SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?',
                       (*values.values(), grant_id))

    @staticmethod
    def _name(grant_id):
        return 'latch-device-' + grant_id.replace('-', '')

    def _public(self, row):
        name = self._name(row['id'])
        return {'grant_id': row['id'], 'device_id': row['device_id'], 'role': row['role'],
                'state': row['state'], 'expires_at': timestamp(row['expires']),
                'ssh_username': name + ':' + name}

    def issue(self, principal, device_id, role, public_key, ttl_seconds=300):
        grant_id = str(uuid.uuid4())
        with self._grant_lock(grant_id):
            if role not in self.permissions.get(principal, {}).get(device_id, ()):
                raise AccessError('Device access is not authorized', 403)
            device = self.inventory.get(device_id)
            if device is None or role not in device.roles:
                raise AccessError('Device access is not authorized', 403)
            if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
                raise AccessError('TTL must be between 1 and 300 seconds', 422)
            public_key = validate_public_key(public_key)
            expires = self.clock() + ttl_seconds
            with self._db() as db:
                db.execute('INSERT INTO grants(id,principal,device_id,role,expires,state) VALUES(?,?,?,?,?,?)',
                           (grant_id, principal, device_id, role, expires, 'creating'))
            lease = None
            try:
                lease = self.bao.issue(role)
                # Persist before provisioning anything downstream so retries can revoke.
                self._update(grant_id, lease_id=lease.lease_id)
                if not lease.lease_id or not lease.username or not lease.password or not 1 <= lease.ttl_seconds <= 300:
                    raise AccessError('Invalid LDAP lease')
                expires = min(expires, self.clock() + lease.ttl_seconds)
                self._update(grant_id, expires=expires)
                self.gateway.provision(self._name(grant_id), device, lease, public_key, timestamp(expires))
                if self.clock() >= expires:
                    raise AccessError('Grant expired during provisioning')
                self._update(grant_id, state='active')
                return self._public(self._row(grant_id))
            except Exception:
                # Never propagate upstream exceptions: they may embed passwords or tokens.
                if lease:
                    # Also handle a failed metadata write immediately after lease issuance.
                    try:
                        self._update(grant_id, lease_id=lease.lease_id)
                    except Exception:
                        try:
                            self.bao.revoke(lease.lease_id)
                        except Exception:
                            pass
                self._cleanup(grant_id)
                raise AccessError('Device access provisioning failed; cleanup scheduled') from None

    def get(self, principal, grant_id):
        with self._grant_lock(grant_id):
            row = self._owned(principal, grant_id)
            if row['state'] == 'active' and row['expires'] <= self.clock():
                self._cleanup(grant_id)
                row = self._row(grant_id)
            return self._public(row)

    def _owned(self, principal, grant_id):
        row = self._row(grant_id)
        if row['principal'] != principal:
            raise AccessError('Grant not found', 404)
        return row

    def revoke(self, principal, grant_id):
        with self._grant_lock(grant_id):
            self._owned(principal, grant_id)
            self._cleanup(grant_id)
            return self._public(self._row(grant_id))

    def _cleanup(self, grant_id):
        row = self._row(grant_id)
        if row['state'] == 'revoked':
            return
        self._update(grant_id, state='revoking', cleanup_attempts=row['cleanup_attempts'] + 1)
        failed = False
        try:
            self.gateway.cleanup(self._name(grant_id))
        except Exception:
            failed = True
        if row['lease_id']:
            try:
                self.bao.revoke(row['lease_id'])
            except Exception:
                failed = True
        self._update(grant_id, state='cleanup_pending' if failed else 'revoked',
                     **({} if failed else {'lease_id': None}))

    def sweep(self):
        with self._db() as db:
            rows = db.execute("SELECT id FROM grants WHERE state NOT IN ('active','revoked') OR (state='active' AND expires<=?)",
                              (self.clock(),)).fetchall()
        cleaned = 0
        for row in rows:
            lock = self._grant_lock(row['id'])
            # An in-flight creation owns its lock; it compensates itself if it
            # expires. A restarted process has no in-flight owners, so its
            # orphaned creating records can be recovered immediately.
            if not lock.acquire(blocking=False):
                continue
            try:
                current = self._row(row['id'])
                # Creation may have completed after the query snapshot.
                if current['state'] == 'revoked' or (current['state'] == 'active' and current['expires'] > self.clock()):
                    continue
                self._cleanup(row['id'])
                cleaned += 1
            finally:
                lock.release()
        return cleaned


class OpenBaoLDAP:
    """Authenticated LDAP-engine client. Token files must be owner-only regular files."""
    def __init__(self, address, token_file, mount='ldap', ca_file=None, timeout=10):
        self.address = address.rstrip('/')
        if urllib.parse.urlsplit(address).scheme not in ('http', 'https'):
            raise ValueError('OpenBao must use HTTP or HTTPS')
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', mount):
            raise ValueError('Invalid LDAP mount')
        self.token_file, self.mount, self.timeout = Path(token_file), mount, timeout
        self.context = ssl.create_default_context(cafile=ca_file)

    def _request(self, method, path, payload=None):
        mode = self.token_file.stat()
        if not stat.S_ISREG(mode.st_mode) or stat.S_IMODE(mode.st_mode) & 0o077:
            raise AccessError('OpenBao token file must have owner-only permissions')
        token = self.token_file.read_text().strip()
        if not token or '\n' in token or '\r' in token:
            raise AccessError('Invalid OpenBao token file')
        request = urllib.request.Request(self.address + '/v1/' + path,
                  data=None if payload is None else json.dumps(payload).encode(), method=method,
                  headers={'X-Vault-Token': token, 'Content-Type': 'application/json'})
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise AccessError('OpenBao redirects are not allowed')
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=self.context))
        try:
            with opener.open(request, timeout=self.timeout) as response:
                data = response.read(1024 * 1024)
                return json.loads(data) if data else {}
        except Exception:
            raise AccessError('OpenBao LDAP request failed') from None

    def issue(self, role):
        if role not in ('readonly', 'operator', 'admin'):
            raise AccessError('Unsupported LDAP role', 422)
        result = self._request('GET', self.mount + '/creds/' + role)
        try:
            return LDAPLease(result['lease_id'], result['data']['username'], result['data']['password'],
                             int(result['lease_duration']))
        except (KeyError, ValueError, TypeError):
            raise AccessError('Invalid OpenBao LDAP response') from None

    def revoke(self, lease_id):
        self._request('PUT', 'sys/leases/revoke', {'lease_id': lease_id})


class WarpgateGateway:
    """Adapter for latch_common.warpgate.Api (pinned TLS, admin cookie authentication).

    Uses unique deterministic names so cleanup recovers objects whose creation
    succeeded remotely but whose response was lost. Only this reserved namespace
    is eligible for cleanup. API client request calls must have a bounded timeout.
    """
    def __init__(self, api):
        self.api = api

    def provision(self, name, device, lease, public_key, expires_at):
        parameters = self.api.request('GET', '/parameters')
        if not parameters.get('recordings_enable') or parameters.get('ssh_host_key_verification') != 'AutoReject':
            raise AccessError('Warpgate must enable recordings and strict host key verification')
        if any(role.get('is_default') for role in self.api.request('GET', '/roles')):
            raise AccessError('Default Warpgate roles must be disabled for isolated device grants')
        kind, key = device.host_public_key.split()[:2]
        known = self.api.request('GET', '/ssh/known-hosts')
        matching = [k for k in known if k['host'] == device.host and k['port'] == device.port]
        if any(k['key_type'] != kind or k['key_base64'] != key for k in matching):
            raise AccessError('Device host key conflicts with pinned gateway key')
        if not matching:
            self.api.request('POST', '/ssh/known-hosts', {'host': device.host, 'port': device.port,
                             'key_type': kind, 'key_base64': key})
        role = self.api.request('POST', '/roles', {'name': name, 'is_default': False})
        user = self.api.request('POST', '/users', {'username': name})
        self.api.request('POST', '/users/' + user['id'] + '/credentials/public-keys',
                         {'label': 'latch-device-key', 'openssh_public_key': public_key})
        target = self.api.request('POST', '/targets', {
            'name': name, 'description': 'Temporary Latch device grant', 'require_approval': False,
            'ticket_requests_disabled': True, 'ticket_require_approval': False,
            'options': {'kind': 'Ssh', 'host': device.host, 'port': device.port,
                        'username': lease.username, 'allow_insecure_algos': False,
                        'auth': {'kind': 'Password', 'password': lease.password}}})
        self.api.request('POST', '/targets/' + target['id'] + '/roles/' + role['id'])
        self.api.request('POST', '/users/' + user['id'] + '/roles/' + role['id'], {'expires_at': expires_at})

    def cleanup(self, name):
        if not re.fullmatch(r'latch-device-[0-9a-f]{32}', name):
            raise AccessError('Invalid grant namespace')
        failed = False
        # Snapshot all pages before closing: closing sessions changes the active list.
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
        except Exception:
            failed = True
        # Deleting a target/user also closes any session admitted during the snapshot.
        for collection, key, endpoint in (('/targets', 'name', '/targets/'),
                                           ('/users', 'username', '/users/'),
                                           ('/roles', 'name', '/role/')):
            try:
                for obj in self.api.request('GET', collection):
                    if obj[key] == name:
                        self._delete_or_close('DELETE', endpoint + obj['id'])
            except Exception:
                failed = True
        if failed:
            raise AccessError('Warpgate cleanup incomplete')

    def _delete_or_close(self, method, path):
        try:
            self.api.request(method, path)
        except (urllib.error.HTTPError, RuntimeError) as error:
            if getattr(error, 'code', None) != 404 and not str(error).endswith('HTTP 404'):
                raise
