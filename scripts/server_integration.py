"""Deployment and laptop client for Latch-managed Server grants.

The OpenStack application credential remains inside Lima. The API bearer token
is stored in owner-only files in Lima and the local deployment state.
SSH private keys remain on the laptop. HTTP requests cross the trusted Lima
control channel as stdin, never as shell commands or secret-bearing argv.
"""
import json
import os
from pathlib import Path
import secrets
import re
import subprocess
from uuid import UUID

import lima_backend as lima
import warpgate


def compose(arguments, options):
    return lima.compose_command(['--profile', 'servers', *arguments], options)


def _write(path, data):
    with open(path, 'w', opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
        stream.write(data)
    path.chmod(0o600)


def retire_legacy_api(options):
    """Stop only the renamed API service; retain its grant volume and credentials."""
    result = subprocess.run(lima.shell('sudo', 'env', '-u', 'DOCKER_HOST', '-u', 'DOCKER_CONTEXT', '-u', 'DOCKER_DEFAULT_PLATFORM', '-u', 'DOCKER_CONFIG', 'docker', 'ps', '-q',
                            '--filter', 'label=com.docker.compose.project=latch-poc',
                            '--filter', 'label=com.docker.compose.service=vm-api'),
                            capture_output=True, text=True, check=True)
    for container in result.stdout.split():
        if not re.fullmatch(r'[a-f0-9]{12,64}', container):
            raise RuntimeError('Unexpected legacy API container identifier')
        subprocess.run(lima.shell('sudo', 'env', '-u', 'DOCKER_HOST', '-u', 'DOCKER_CONTEXT', '-u', 'DOCKER_DEFAULT_PLATFORM', '-u', 'DOCKER_CONFIG', 'docker', 'stop', container), check=True)


def _inventory(inventory):
    # Preserve existing owner-managed authorization while adopting server naming.
    if 'servers' not in inventory and 'vms' in inventory:
        inventory = {**inventory, 'servers': inventory['vms']}
        del inventory['vms']
    return inventory


def up(root, state, options, server_host_public_key, gateway):
    directory = Path(state) / 'vm'
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    # Build before migration so dependency-download failure does not interrupt
    # the previously working path.
    subprocess.run(compose(['build', 'server-api', 'latch-api'], options), cwd=root, check=True)
    active = gateway.request('GET', '/sessions?username=latch&active_only=true&limit=1')['items']
    if active:
        raise RuntimeError('Close the existing shared server session before provisioning API-managed access')
    script = ('source /home/stack/latch/guest/common.sh; os_auth; '
              'python3 /home/stack/latch/guest/server-api-prepare.py')
    result = subprocess.run(lima.shell('sudo', 'bash', '-c', script),
                            capture_output=True, text=True, check=True)
    server = json.loads(result.stdout)
    server['host_public_key'] = server_host_public_key
    server_id = server.pop('server_id')
    result = subprocess.run(compose(['exec', '-T', 'warpgate', 'ssh-keyscan',
                                    '-T', '5', '-t', 'ed25519', '-p', '2222', '127.0.0.1'], options),
                            capture_output=True, text=True, check=True)
    keys = [line.split() for line in result.stdout.splitlines() if not line.startswith('#')]
    if len(keys) != 1 or len(keys[0]) < 3 or keys[0][1] != 'ssh-ed25519':
        raise RuntimeError('Trusted gateway host-key export failed')
    gateway_key = ' '.join(keys[0][1:3])
    known = Path(state) / 'warpgate_known_hosts'
    if known.exists():
        for line in known.read_text().splitlines():
            fields = line.split()
            if len(fields) >= 3 and fields[0] == 'latch-warpgate' and ' '.join(fields[1:3]) != gateway_key:
                raise RuntimeError('Gateway SSH host key changed; review its identity before replacing the pin')
    _write(known, 'latch-warpgate ' + gateway_key + '\n')
    inventory = {'servers': {server_id: server}, 'permissions': {'lab-operator': [server_id]},
                 'gateway': {'host': '127.0.0.1', 'port': int(options.get('LAB_WORKER_PORT', 9202)),
                             'host_public_key': gateway_key}}
    inventory_path = directory / 'inventory.json'
    # Refuse to overwrite operator-maintained authorization or key pins.
    if inventory_path.exists() and _inventory(json.loads(inventory_path.read_text())) != inventory:
        raise RuntimeError('Server API inventory changed; review the instance IDs, permissions and host pins explicitly')
    _write(inventory_path, json.dumps(inventory, indent=2) + '\n')
    token = directory / 'api-token'
    if not token.exists():
        _write(token, secrets.token_urlsafe(40))
    remote = f'{lima.REMOTE}/.state/vm'
    subprocess.run(lima.shell('sudo', 'mkdir', '-p', remote), check=True)
    subprocess.run(lima.shell('sudo', 'chmod', '700', remote), check=True)
    for name in ('inventory.json', 'api-token'):
        subprocess.run(lima.shell('sudo', 'tee', f'{remote}/{name}'),
                       input=(directory / name).read_bytes(), stdout=subprocess.DEVNULL, check=True)
        subprocess.run(lima.shell('sudo', 'chmod', '600', f'{remote}/{name}'), check=True)
    subprocess.run(lima.shell('sudo', 'tee', f'{lima.REMOTE}/.state/warpgate/tls.certificate.pem'),
                   input=(Path(state) / 'warpgate/tls.certificate.pem').read_bytes(),
                   stdout=subprocess.DEVNULL, check=True)
    retire_legacy_api(options)
    subprocess.run(compose(['up', '-d', '--no-deps', '--force-recreate', '--wait',
                           '--wait-timeout', '120', 'server-api', 'latch-api'], options), cwd=root, check=True)
    # Admission goes away before the old shared network path is retired.
    for collection, key, name, endpoint in (
            ('/targets', 'name', 'latch-vm', '/targets/'),
            ('/roles', 'name', 'latch-vm-access', '/role/')):
        for obj in gateway.request('GET', collection):
            if obj[key] == name:
                gateway.request('DELETE', endpoint + obj['id'])
    warpgate.close_sessions(gateway)
    for user in gateway.request('GET', '/users'):
        if user['username'] == 'latch':
            gateway.request('DELETE', '/users/' + user['id'])
    subprocess.run(lima.shell('sudo', 'bash', '-c', script + ' --retire-legacy'),
                   capture_output=True, text=True, check=True)
    print('Latch Server API ready: isolated grants, certificate-backed SSH and automatic expiry.')


def request(options, method, path, payload=None, *, idempotency_key=None):
    script = '''import json,pathlib,sys,urllib.request,urllib.error
p=json.load(sys.stdin)
token=pathlib.Path('/run/server-api/api-token').read_text().strip()
headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'}
if p.get('idempotency_key'): headers['Idempotency-Key']=p['idempotency_key']
r=urllib.request.Request('http://127.0.0.1:8320'+p['path'],method=p['method'],
 data=None if p['payload'] is None else json.dumps(p['payload']).encode(),headers=headers)
try:
 with urllib.request.urlopen(r,timeout=120) as response: print(response.read().decode())
except urllib.error.HTTPError as error:
 print('Server API HTTP '+str(error.code),file=sys.stderr);sys.exit(1)
'''
    try:
        result = subprocess.run(compose(['exec', '-T', '--user', '10001:10001', 'server-api',
                                         'python', '-c', script], options),
                                input=json.dumps({'method': method, 'path': path, 'payload': payload,
                                                  'idempotency_key': idempotency_key}),
                                capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as error:
        status = re.search(r'Server API HTTP ([0-9]{3})', error.stderr or '')
        if status:
            raise RuntimeError('Server API request failed: HTTP ' + status.group(1)) from None
        raise RuntimeError('Could not contact the Server API; check ./lab status') from None
    return json.loads(result.stdout)


def issue(state, options, server_id=None, ttl_seconds=300):
    if server_id is None:
        inventory = json.loads((Path(state) / 'vm/inventory.json').read_text())
        if len(inventory.get('servers', inventory.get('vms', {}))) != 1:
            raise RuntimeError('Select a server explicitly with --server')
        server_id = next(iter(inventory.get('servers', inventory.get('vms', {}))))
    payload = {'server_id': str(UUID(server_id)), 'ttl_seconds': ttl_seconds,
               'public_key': (Path(state) / 'gateway_ed25519.pub').read_text().strip()}
    return request(options, 'POST', '/v1/server-grants', payload, idempotency_key=secrets.token_urlsafe(24))


def current(state, options):
    path = Path(state) / 'vm/current-grant.json'
    if not path.exists():
        return None
    cached = json.loads(path.read_text())
    grant = request(options, 'GET', '/v1/server-grants/' + str(UUID(cached['grant_id'])))
    return grant if grant['state'] == 'active' else None


def remember(state, grant):
    _write(Path(state) / 'vm/current-grant.json', json.dumps({'grant_id': grant['grant_id']}))


def revoke(state, options, grant_id=None):
    if grant_id is None:
        grant = current(state, options)
        if grant is None:
            raise RuntimeError('No active default Server grant; use server-revoke --grant UUID for another grant')
        grant_id = grant['grant_id']
    return request(options, 'DELETE', '/v1/server-grants/' + str(UUID(grant_id)))


def ssh_command(state, options, grant, command=None, *, tty=False):
    if grant['state'] != 'active':
        raise RuntimeError('Server grant is not active')
    # Pin the ingress key supplied through the trusted API channel as well as
    # the independently-pinned target key verified inside Warpgate.
    public_key = grant['gateway_host_public_key']
    fields = public_key.split()
    if len(fields) < 2 or '\n' in public_key or fields[0] != 'ssh-ed25519':
        raise RuntimeError('Invalid gateway SSH pin in Server grant')
    state = Path(state)
    known = state / 'warpgate_known_hosts'
    expected = 'latch-warpgate ' + ' '.join(fields[:2]) + '\n'
    if known.exists() and known.read_text() != expected:
        raise RuntimeError('Gateway SSH host key conflicts with local pin')
    _write(known, expected)
    arguments = warpgate.ssh_command(state, options, tty=tty)
    arguments[arguments.index('-p') + 1] = str(grant['gateway_port'])
    arguments[arguments.index('StrictHostKeyChecking=accept-new')] = 'StrictHostKeyChecking=yes'
    arguments[-1] = grant['ssh_username'] + '@' + grant['gateway_host']
    if command is not None:
        arguments.append(command)
    return arguments
