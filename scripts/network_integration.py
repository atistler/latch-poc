"""Optional network-device lab with a routed, isolated device-management subnet."""
import base64
import json
import os
import secrets
import subprocess
import sys
import tempfile

import device_integration as device
import lima_backend as lima

DEVICE = 'freertr'
HOST_KEY = '/var/lib/freertr/hostkey.pub'


def compose(args, options):
    return lima.compose_command(['--profile', 'device', '--profile', 'network', *args], options)


def prepare(root, state):
    directory = device.prepare(root, state)
    path = directory / 'tacacs-secret'
    if path.is_symlink():
        raise RuntimeError('TACACS secret must be a private regular file')
    if not path.exists():
        with open(path, 'x', opener=lambda p, f: os.open(p, f, 0o600)) as stream:
            stream.write(secrets.token_hex(32))
    value = path.read_text()
    if len(value) < 32 or len(value) > 128 or not value.isascii() or not value.isalnum():
        raise RuntimeError('Invalid TACACS shared secret')
    path.chmod(0o600)
    return directory


def sync(root, state):
    directory = prepare(root, state)
    device.sync(root, state)
    path = directory / 'tacacs-secret'
    target = f'{lima.REMOTE}/.state/device/tacacs-secret'
    subprocess.run(lima.shell('sudo', 'tee', target), input=path.read_bytes(),
                   stdout=subprocess.DEVNULL, check=True)
    subprocess.run(lima.shell('sudo', 'chmod', '600', target), check=True)


def merge_inventory(root, state, public_key):
    """Accept a management-exported key; refuse changing an established pin."""
    fields = public_key.strip().split()
    if len(fields) < 2 or fields[0] not in ('ssh-rsa', 'ssh-ed25519', 'ecdsa-sha2-nistp256'):
        raise RuntimeError('Router did not export an OpenSSH host key')
    try:
        base64.b64decode(fields[1], validate=True)
    except ValueError:
        raise RuntimeError('Invalid router host key') from None
    public_key = ' '.join(fields[:2])
    path = prepare(root, state) / 'inventory.json'
    config = json.loads(path.read_text())
    roles = ['readonly', 'operator', 'admin']
    device_config = {'host': DEVICE, 'port': 22, 'host_public_key': public_key,
                     'roles': roles}
    old_device = dict(device_config, roles=['readonly', 'operator'])
    existing = config.setdefault('devices', {}).get(DEVICE)
    if existing is not None and existing not in (device_config, old_device):
        raise RuntimeError('Existing freertr inventory differs; review it before replacing its host pin')
    permissions = config.setdefault('permissions', {}).setdefault('lab-operator', {})
    current_roles = permissions.get(DEVICE)
    if current_roles is not None and current_roles not in (roles, old_device['roles']):
        raise RuntimeError('Existing freertr operator permissions differ; review before changing policy')
    # Expand only the exact generated two-role policy, never custom restrictions.
    migrating = existing == old_device or current_roles == old_device['roles']
    if migrating:
        backup = path.with_name('inventory.before-admin.json')
        if not backup.exists():
            atomic_write(backup, path.read_text())
    config['devices'][DEVICE] = device_config
    permissions[DEVICE] = roles.copy()
    atomic_write(path, json.dumps(config, indent=2) + '\n')


def atomic_write(path, content):
    """Publish owner-only inventory or migration backup after a complete write."""
    descriptor, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    temporary = path.with_name(os.path.basename(name))
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def up(root, state, options):
    sync(root, state)
    subprocess.run(compose(['build', 'warpgate', 'device-router', 'tacacs', 'freertr'], options), check=True, cwd=root)
    # Recreate the gateway to apply its management address, route and host alias.
    # Its identity, recordings and target state remain on existing volumes.
    subprocess.run(compose(['up', '-d', '--no-deps', '--wait', '--wait-timeout', '120',
                           'device-router', 'warpgate'], options), check=True, cwd=root)
    device.up(root, state, options)
    subprocess.run(compose(['up', '-d', '--no-deps', '--wait', '--wait-timeout', '180',
                           'tacacs', 'freertr'], options), check=True, cwd=root)
    exported = subprocess.run(compose(['exec', '-T', 'freertr', 'cat', HOST_KEY], options),
                              capture_output=True, text=True, check=True, cwd=root)
    merge_inventory(root, state, exported.stdout)
    device.sync(root, state)
    subprocess.run(compose(['up', '-d', '--no-deps', '--force-recreate', '--wait',
                           '--wait-timeout', '120', 'device-api'], options), check=True, cwd=root)
    print('Network lab ready: FreeRtr SSH, LDAP-backed TACACS+, pinned host key, readonly/operator/admin grants')


def verify(root, state, options):
    # Run real LDAP lifecycle checks before the real SSH/authorization harness.
    device.verify(root, options)
    subprocess.run([sys.executable, str(root / 'tests/live_network_device.py')], check=True, cwd=root)


def down(root, state, options):
    # Never compose down: the profiles share the foundation and its volumes.
    subprocess.run(compose(['stop', 'freertr', 'tacacs', 'device-router'], options), check=True, cwd=root)
    print('Network devices stopped; keys, inventory and foundation retained')
