#!/usr/bin/env python3
"""Bootstrap only the isolated LDAP OpenBao; never print secrets or LDAP values."""
import hashlib
import json
from pathlib import Path
import urllib.error
import urllib.request

DATA = Path('/opt/latch/bao')
SECRETS = Path('/run/device-secrets')
BASE = 'http://127.0.0.1:8200/v1/'


def request(path, data=None, method=None):
    token = json.loads((DATA / 'init.json').read_text())['root_token']
    req = urllib.request.Request(BASE + path, data=None if data is None else json.dumps(data).encode(),
                                 method=method, headers={'X-Vault-Token': token, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as response:
        body = response.read()
    return json.loads(body) if body else {}


def main():
    ready = DATA / 'ldap-ready'
    ready.unlink(missing_ok=True)
    candidates = [p for p in Path('/opt/latch/plugins').rglob('*') if p.is_file() and p.name.startswith('openbao-plugin-secrets-ldap') and not p.suffix]
    if len(candidates) != 1:
        raise RuntimeError('Expected one pinned LDAP plugin executable')
    plugin = candidates[0]
    request('sys/plugins/catalog/secret/ldap', {
        'sha256': hashlib.sha256(plugin.read_bytes()).hexdigest(),
        'command': str(plugin.relative_to('/opt/latch/plugins')),
    }, 'PUT')
    mounts = request('sys/mounts')['data']
    if 'ldap/' not in mounts:
        try:
            request('sys/mounts/ldap', {'type': 'ldap'}, 'POST')
        except urllib.error.HTTPError as error:
            # This request contains no credentials; include plugin load diagnostics.
            raise RuntimeError('LDAP plugin mount failed: ' + error.read().decode()) from None
    request('ldap/config', {
        'url': 'ldaps://openldap:636',
        'binddn': 'cn=openbao,ou=services,dc=latch,dc=lab',
        'bindpass': (SECRETS / 'ldap-openbao-password').read_text().strip(),
        'certificate': Path('/etc/latch-tls/ca.pem').read_text(),
        'insecure_tls': False,
        'userdn': 'ou=users,dc=latch,dc=lab',
        'userattr': 'uid',
        'schema': 'openldap',
    }, 'POST')
    templates = Path('/usr/local/share/latch/ldap')
    for role in ('admin', 'operator', 'readonly'):
        creation = (templates / 'creation.ldif').read_text().replace('ROLE', role)
        deletion = (templates / 'deletion.ldif').read_text().replace('ROLE', role)
        request('ldap/role/' + role, {
            'creation_ldif': creation, 'deletion_ldif': deletion,
            'rollback_ldif': deletion,
            'default_ttl': '5m', 'max_ttl': '5m',
            'username_template': 'l{{random 15}}',
        }, 'POST')
    policy = '''path "ldap/creds/admin" { capabilities = ["read"] }
path "ldap/creds/operator" { capabilities = ["read"] }
path "ldap/creds/readonly" { capabilities = ["read"] }
path "sys/leases/revoke" { capabilities = ["update"] }
'''
    request('sys/policies/acl/latch-device', {'policy': policy}, 'PUT')
    # The CLI pre-generates this file for the non-root device API container.
    desired = (SECRETS / 'device-bao-token').read_text().strip()
    try:
        existing = request('auth/token/lookup', {'token': desired}, 'POST')
    except urllib.error.HTTPError as error:
        if error.code not in (400, 403, 404):
            raise
        existing = None
    if existing is None:
        request('auth/token/create', {'id': desired, 'policies': ['latch-device'], 'no_default_policy': True,
                                     'renewable': False, 'ttl': '0', 'display_name': 'latch-device-api'}, 'POST')
    elif set(existing['data']['policies']) != {'latch-device'}:
        raise RuntimeError('Existing device token has unexpected privileges')
    ready.touch(mode=0o600)
    print('Isolated LDAP OpenBao LDAP engine ready; roles admin/operator/readonly, TTL 5m.')


if __name__ == '__main__':
    main()
