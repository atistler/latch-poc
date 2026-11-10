#!/usr/bin/env python3
"""Real LDAP bind, ACL, group cleanup and lease expiry tests; no secret output."""
import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

BASE = 'http://127.0.0.1:8200/v1/'
SECRETS = Path('/run/device-secrets')


def api(path, body=None):
    req = urllib.request.Request(BASE + path, data=None if body is None else json.dumps(body).encode(),
        headers={'X-Vault-Token': (SECRETS / 'device-bao-token').read_text().strip(), 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=15) as response:
        data = response.read()
    return json.loads(data) if data else {}


def ldap(command, dn, password_file, *args, input=None, ok=True):
    env = dict(os.environ, LDAPTLS_CACERT='/etc/latch-tls/ca.pem')
    result = subprocess.run([command, '-x', '-H', 'ldaps://openldap:636', '-D', dn, '-y', str(password_file), *args],
                            input=input, capture_output=True, text=True, env=env)
    if ok and result.returncode:
        raise RuntimeError(f'{command} failed with LDAP status {result.returncode}')
    return result


def absent(username):
    result = ldap('ldapsearch', 'cn=reader,ou=services,dc=latch,dc=lab', SECRETS / 'ldap-reader-password',
                  '-LLL', '-b', 'dc=latch,dc=lab', f'(|(uid={username})(member=uid={username},ou=users,dc=latch,dc=lab))', 'dn')
    return 'dn:' not in result.stdout


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--expiry', action='store_true', help='Wait for the actual 5 minute lease TTL')
    args = parser.parse_args()
    for role in ('admin', 'operator', 'readonly'):
        issued = api('ldap/creds/' + role)
        username = issued['data']['username']
        dn = 'uid=' + username + ',ou=users,dc=latch,dc=lab'
        try:
            with tempfile.NamedTemporaryFile(mode='w+', encoding='utf-8') as password:
                password.write(issued['data']['password'])
                password.flush()
                ldap('ldapwhoami', dn, password.name)
                # The reader bind reads users and groups but cannot mutate directory entries.
                result = ldap('ldapsearch', 'cn=reader,ou=services,dc=latch,dc=lab', SECRETS / 'ldap-reader-password',
                              '-LLL', '-b', 'cn=' + role + ',ou=groups,dc=latch,dc=lab', '(objectClass=*)', 'member')
                if dn not in result.stdout:
                    raise RuntimeError('Leased identity missing expected role membership')
                stored = ldap('ldapsearch', 'cn=openbao,ou=services,dc=latch,dc=lab', SECRETS / 'ldap-openbao-password',
                              '-LLL', '-o', 'ldif-wrap=no', '-b', dn, '(objectClass=*)', 'userPassword')
                # LDAP utilities base64-encode password hashes; compare decoded values.
                hashes = []
                for line in stored.stdout.splitlines():
                    if line.startswith('userPassword:: '):
                        hashes.append(base64.b64decode(line.partition(':: ')[2]))
                    elif line.startswith('userPassword: '):
                        hashes.append(line.partition(': ')[2].encode())
                if not hashes or not hashes[0].startswith(b'{SSHA}'):
                    raise RuntimeError('Leased userPassword was not hashed by OpenLDAP')
                result = ldap('ldapmodify', 'cn=reader,ou=services,dc=latch,dc=lab', SECRETS / 'ldap-reader-password',
                              input=f'dn: {dn}\nchangetype: modify\nreplace: sn\nsn: forbidden\n', ok=False)
                if result.returncode != 50:
                    raise RuntimeError('LDAP reader was not denied mutation')
                result = ldap('ldapmodify', 'cn=openbao,ou=services,dc=latch,dc=lab', SECRETS / 'ldap-openbao-password',
                              input='dn: cn=reader,ou=services,dc=latch,dc=lab\nchangetype: modify\nreplace: userPassword\nuserPassword: forbidden\n', ok=False)
                if result.returncode != 50:
                    raise RuntimeError('OpenBao bind could mutate service credentials')
                if args.expiry and role == 'readonly':
                    deadline = time.monotonic() + issued['lease_duration'] + 90
                    while not absent(username):
                        if time.monotonic() > deadline:
                            raise RuntimeError('Expired LDAP lease not deleted within 90s')
                        time.sleep(3)
                else:
                    api('sys/leases/revoke', {'lease_id': issued['lease_id']})
                if not absent(username):
                    raise RuntimeError('Revoked entry or group membership remains')
                result = ldap('ldapwhoami', dn, password.name, ok=False)
                if result.returncode != 49:
                    raise RuntimeError('Revoked identity still authenticates')
        finally:
            api('sys/leases/revoke', {'lease_id': issued['lease_id']})
        print(f'PASS {role}: issuance, LDAPS bind, group membership, reader ACL, cleanup, denied bind' + (' (TTL expiry)' if args.expiry and role == 'readonly' else ' (explicit revoke)'))


if __name__ == '__main__':
    main()
