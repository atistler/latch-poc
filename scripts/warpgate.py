"""Warpgate v0.30 admin API and local SSH access helpers.

The gateway owns its outgoing SSH key. OpenBao signs that key at connection
time; the laptop's distinct key authenticates only to the gateway.
"""
import base64
import json
import os
from pathlib import Path
import secrets
import subprocess

from latch_common.warpgate import Api


USERNAME = 'latch'
TARGET = 'latch-vm'
BACKEND = 'latch-openbao'


def prepare(state):
    state = Path(state)
    directory = state / 'warpgate'
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    password = directory / 'admin-password'
    if not password.exists():
        with open(password, 'x', opener=lambda p, f: os.open(p, f, 0o600)) as stream:
            stream.write(secrets.token_urlsafe(32) + '\n')
    password.chmod(0o600)
    key = state / 'gateway_ed25519'
    if not key.exists():
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C',
                        'latch-warpgate-client', '-f', str(key)], check=True)
    key.chmod(0o600)


def _upsert(api, path, data, *, key='name'):
    existing = next((item for item in api.request('GET', path)
                     if item[key] == data[key]), None)
    if existing:
        return api.request('PUT', f'{path}/{existing["id"]}', data)
    return api.request('POST', path, data)


def bootstrap(api, state, *, role_id, secret_id, host, host_public_key, port=22,
              bao_address='http://openbao:8200', bao_mount='ssh-client-signer',
              bao_role='latch-warpgate', isolated=False):
    """Reconcile just the lab user, access role, signer and SSH target."""
    key_type, key_base64, *_ = host_public_key.strip().split()
    if not key_type.startswith(('ssh-', 'ecdsa-')):
        raise ValueError('Expected an OpenSSH host public key')
    base64.b64decode(key_base64, validate=True)
    if not isolated:
        role = next((r for r in api.request('GET', '/roles') if r['name'] == 'latch-vm-access'), None)
        if role is None:
            role = api.request('POST', '/roles', {'name': 'latch-vm-access', 'is_default': False})
        user = next((u for u in api.request('GET', '/users') if u['username'] == USERNAME), None)
        if user is None:
            user = api.request('POST', '/users', {'username': USERNAME})
        user_id = user['id']
        keypath = f'/users/{user_id}/credentials/public-keys'
        _upsert(api, keypath, {'label': 'latch-laptop',
            'openssh_public_key': (Path(state) / 'gateway_ed25519.pub').read_text().strip()}, key='label')
        api.request('POST', f'/users/{user_id}/roles/{role["id"]}', {})
    backend = _upsert(api, '/secret-backends', {
        'name': BACKEND, 'backend_type': 'openbao', 'address': bao_address,
        'namespace': None, 'tls_skip_verify': False,
        'allowed_paths': [f'{bao_mount}/sign/{bao_role}'],
        'auth': {'method': 'AppRole', 'role_id': role_id, 'secret_id': secret_id, 'mount': 'approle'},
    })
    health = api.request('POST', f'/secret-backends/{backend["id"]}/health')
    if health and health.get('error'):
        raise RuntimeError('Warpgate OpenBao backend health check failed')
    if not isolated:
        target = _upsert(api, '/targets', {
            'name': TARGET, 'description': 'OpenStack VM with OpenBao SSH certificates',
            'require_approval': False, 'ticket_requests_disabled': True, 'ticket_require_approval': False,
            'options': {'kind': 'Ssh', 'host': host, 'port': port, 'username': 'ubuntu',
                        'allow_insecure_algos': False, 'auth': {'kind': 'Certificate',
                        'backend': BACKEND, 'mount': bao_mount, 'role': bao_role}},
        })
    # Refuse changed keys instead of silently trusting a rebuilt or impersonated VM.
    pinned = [k for k in api.request('GET', '/ssh/known-hosts')
              if k['host'] == host and k['port'] == port]
    if any(k['key_type'] == key_type and k['key_base64'] != key_base64 for k in pinned):
        raise RuntimeError('Warpgate target host key changed; review and remove the old pin explicitly')
    if not any(k['key_type'] == key_type and k['key_base64'] == key_base64 for k in pinned):
        api.request('POST', '/ssh/known-hosts', {'host': host, 'port': port,
                    'key_type': key_type, 'key_base64': key_base64})
    if not isolated:
        assigned = api.request('GET', f'/targets/{target["id"]}/roles')
        if not any(r['id'] == role['id'] for r in assigned):
            api.request('POST', f'/targets/{target["id"]}/roles/{role["id"]}')
    api.request('PUT', '/parameters', {'recordings_enable': True,
                'ssh_host_key_verification': 'AutoReject', 'ssh_client_auth_publickey': True})
    return {} if isolated else {'target_id': target['id'], 'user_id': user_id, 'role_id': role['id']}


def ssh_command(state, settings, command=None, *, tty=False):
    state = Path(state)
    # Quote SSH config values, which OpenSSH parses again after argv processing.
    known_hosts = str(state / 'warpgate_known_hosts').replace('"', '\\"')
    result = ['ssh', '-tt' if tty else '-T', '-p', str(settings.get('LAB_WORKER_PORT', '9202')),
        '-i', str(state / 'gateway_ed25519'), '-o', 'IdentityAgent=none',
        '-o', 'IdentitiesOnly=yes', '-o', 'PasswordAuthentication=no',
        '-o', 'KbdInteractiveAuthentication=no', '-o', 'BatchMode=yes',
        '-o', 'ConnectTimeout=30', '-o', 'StrictHostKeyChecking=accept-new',
        '-o', f'UserKnownHostsFile="{known_hosts}"', '-o', 'HostKeyAlias=latch-warpgate',
        f'{USERNAME}:{TARGET}@127.0.0.1']
    if command is not None:
        result.append(command)
    return result


def verify_recording(api, marker, *, username=USERNAME, target_name=TARGET):
    """Require a replayable terminal recording containing the actual SSH output."""
    from urllib.parse import urlencode
    sessions = api.request('GET', '/sessions?' + urlencode({'username': username, 'limit': 100}))['items']
    for session in sessions:
        if not any(t.get('target', {}).get('name') == target_name
                   for t in session.get('target_sessions', []) if t.get('target')):
            continue
        for recording in api.request('GET', f'/sessions/{session["id"]}/recordings'):
            data = api.request('GET', f'/recordings/{recording["id"]}/data', raw=True)
            output = bytearray()
            for line in data.splitlines():
                event = json.loads(line)
                if 'data' in event and event.get('stream', 'Output') in ('Output', 'Error'):
                    output.extend(base64.b64decode(event['data']))
            if marker.encode() in output:
                return {'session_id': session['id'], 'recording_id': recording['id']}
    raise RuntimeError('No Warpgate SSH recording contains the verification output')


def close_sessions(api):
    """Close only live SSH sessions of the lab user, preserving admin sessions."""
    sessions = api.request('GET', '/sessions?username=latch&active_only=true&limit=100')['items']
    count = 0
    for session in sessions:
        if session['protocol'] == 'SSH' or session['protocol'].lower() == 'ssh':
            api.request('POST', f'/sessions/{session["id"]}/close')
            count += 1
    return count
