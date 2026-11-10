"""OpenBao HTTP client. User private keys are generated and retained locally."""
import json
import os
from pathlib import Path
import subprocess
import urllib.error
import urllib.request


class BaoError(RuntimeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(f'OpenBao HTTP {status}: {message}')


def api(address, path, payload, token=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['X-Vault-Token'] = token
    request = urllib.request.Request(address + '/v1/' + path, data=json.dumps(payload).encode(), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        # Do not include request data or tokens in exceptions.
        detail = json.loads(error.read()).get('errors', ['request rejected'])
        raise BaoError(error.code, '; '.join(detail)) from None


def login(address):
    return api(address, 'auth/userpass/login/latch', {'password': 'latch-local-only'})['auth']['client_token']


def user_key(state):
    state = Path(state)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    state.chmod(0o700)
    key = state / 'user_ed25519'
    if not key.exists():
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
    key.chmod(0o600)
    return key


def issue(address, state, ttl='15m', principal='ubuntu', name='ssh-user-cert.pub'):
    key = user_key(state)
    token = login(address)
    data = api(address, 'ssh-client-signer/sign/latch-user', {
        'public_key': key.with_suffix('.pub').read_text().strip(),
        'valid_principals': principal, 'ttl': ttl,
    }, token)['data']
    certificate = Path(state) / name
    with os.fdopen(os.open(certificate, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as stream:
        stream.write(data['signed_key'].strip() + '\n')
    return key, certificate
