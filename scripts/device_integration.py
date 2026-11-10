"""Shared LDAP/OpenBao foundation and API lifecycle for network-device access."""
import json
import os
from pathlib import Path
import secrets
import subprocess

import lima_backend as lima

SECRET_FILES = ('ldap-admin-password', 'ldap-openbao-password', 'ldap-reader-password',
                'device-bao-token', 'api-token')

def compose(args, options):
    return lima.compose_command(['--profile', 'device', *args], options)



def migrate_state(state, directory):
    """Copy known files from the previous state layout without overwriting data."""
    previous = state / 'ise'
    if not previous.exists():
        return
    if previous.is_symlink() or not previous.is_dir():
        raise RuntimeError('Previous device state must be a regular directory')
    names = {name: name for name in (*SECRET_FILES, 'inventory.json', 'tacacs-secret')}
    names['ldap-reader-password'] = 'ldap-ise-password'
    for name, old_name in names.items():
        target, source = directory / name, previous / old_name
        if target.exists() or target.is_symlink() or not source.exists():
            continue
        if source.is_symlink() or not source.is_file() or source.stat().st_size > 65536:
            raise RuntimeError('Invalid previous device configuration file: ' + old_name)
        with open(target, 'xb', opener=lambda p, f: os.open(p, f, 0o600)) as stream:
            stream.write(source.read_bytes())


def prepare(root, state):
    directory = state / 'device'
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    migrate_state(state, directory)
    for name in SECRET_FILES:
        path = directory / name
        if not path.exists():
            with open(path, 'x', opener=lambda p, f: os.open(p, f, 0o600)) as stream:
                stream.write(secrets.token_urlsafe(40))
        # slapd's password-file reader hashes exact bytes; keep this contract
        # identical to the API clients, which strip surrounding whitespace.
        if path.read_bytes().endswith(b'\n'):
            path.write_bytes(path.read_bytes().rstrip(b'\r\n'))
        path.chmod(0o600)
    inventory = directory / 'inventory.json'
    if not inventory.exists():
        inventory.write_bytes((root / 'config/device/inventory.example.json').read_bytes())
    inventory.chmod(0o600)
    return directory


def sync(root, state):
    directory = prepare(root, state)
    remote = f'{lima.REMOTE}/.state/device'
    subprocess.run(lima.shell('sudo', 'mkdir', '-p', remote), check=True)
    subprocess.run(lima.shell('sudo', 'chmod', '700', remote), check=True)
    # Copy only known configuration files, never arbitrary state or disk images.
    for name in (*SECRET_FILES, 'inventory.json'):
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            raise RuntimeError('Invalid or oversized device configuration file: ' + name)
        target = f'{remote}/{path.name}'
        subprocess.run(lima.shell('sudo', 'tee', target), input=path.read_bytes(),
                       stdout=subprocess.DEVNULL, check=True)
        subprocess.run(lima.shell('sudo', 'chmod', '600', target), check=True)



def stop_previous_openbao():
    """Retire the exact old service before reusing its existing Raft volume."""
    name = 'latch-poc-ise-openbao-1'
    existing = subprocess.run(lima.shell('sudo', 'docker', 'ps', '-a',
        '--filter', f'name=^/{name}$',
        '--filter', 'label=com.docker.compose.project=latch-poc',
        '--filter', 'label=com.docker.compose.service=ise-openbao',
        '--format', '{{.ID}}'), capture_output=True, text=True, check=True)
    if existing.stdout.strip():
        # Do not remove orphans or volumes. A failed stop blocks the replacement.
        subprocess.run(lima.shell('sudo', 'docker', 'stop', name), check=True)


def up(root, state, options):
    sync(root, state)
    # Pin already established by ./lab provision. Never fetch a gateway key
    # over an untrusted network to make this optional stack start successfully.
    pin = state / 'warpgate/tls.certificate.pem'
    if not pin.exists():
        raise RuntimeError('Configure the existing gateway first with ./lab provision')
    subprocess.run(lima.shell('sudo', 'tee', f'{lima.REMOTE}/.state/warpgate/tls.certificate.pem'),
                   input=pin.read_bytes(), stdout=subprocess.DEVNULL, check=True)
    subprocess.run(compose(['build', 'openldap', 'ldap-openbao', 'device-api'], options), check=True, cwd=root)
    stop_previous_openbao()
    subprocess.run(compose(['up', '-d', '--no-deps', '--wait', '--wait-timeout', '300',
                           'openldap', 'ldap-openbao'], options), check=True, cwd=root)
    # Bind-mounted inventory changes do not change the Compose service hash.
    # Recreate only this optional service to reload operator policy and secrets.
    subprocess.run(compose(['up', '-d', '--no-deps', '--force-recreate', '--wait',
                           '--wait-timeout', '120', 'device-api'], options), check=True, cwd=root)


def request(options, method, path, payload=None):
    # The bearer token stays inside its private container filesystem. Neither
    # token nor generated LDAP password travels through command arguments.
    script = '''import json,pathlib,sys,urllib.request,urllib.error
p=json.load(sys.stdin)
token=pathlib.Path('/run/device-api/api-token').read_text().strip()
r=urllib.request.Request('http://127.0.0.1:8310'+p['path'],method=p['method'],
 data=None if p['payload'] is None else json.dumps(p['payload']).encode(),
 headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
try:
 with urllib.request.urlopen(r,timeout=90) as response: print(response.read().decode())
except urllib.error.HTTPError as error:
 print('Device API HTTP '+str(error.code),file=sys.stderr);sys.exit(1)
'''
    result = subprocess.run(compose(['exec', '-T', '--user', '10001:10001', 'device-api',
                                     'python', '-c', script], options),
                            input=json.dumps({'method': method, 'path': path, 'payload': payload}),
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def verify(root, options):
    subprocess.run(compose(['exec', '-T', 'ldap-openbao', 'python3',
                           '/usr/local/lib/latch/ldap-validate.py'], options), check=True, cwd=root)
    result = request(options, 'GET', '/health/live')
    if result != {'status': 'alive'}:
        raise RuntimeError('Device API liveness failed')
    print('PASS: LDAP dynamic credentials and authenticated device API are running')
