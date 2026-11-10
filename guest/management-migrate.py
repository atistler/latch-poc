#!/usr/bin/env python3
"""Cold-back up OpenBao before recreating the lab's management network."""
import argparse
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request

NETWORK = 'latch-poc_management'
OLD = '172.20.1.0/24'
NEW = '10.1.3.0/24'
ROOT = Path('/opt/latch/backups/mgmt')
PENDING = ROOT / 'pending.json'
BASE = Path('/home/stack/latch')


def run(*command, **kwargs):
    return subprocess.run(command, check=True, text=True, capture_output=True, **kwargs).stdout


def check_routes(routes):
    target = ipaddress.ip_network(NEW)
    for route in routes:
        destination = route.get('dst', 'default')
        if destination == 'default':
            continue
        if ipaddress.ip_network(destination, strict=False).overlaps(target):
            raise RuntimeError(f'Linux route {destination} overlaps {NEW}; resolve it before migration')


def check_container(container):
    labels = container.get('Config', {}).get('Labels') or {}
    if (labels.get('com.docker.compose.project') != 'latch-poc'
            or labels.get('com.docker.compose.service') not in {'openbao', 'latch-api'}):
        raise RuntimeError('Unexpected container attached to management network; detach it explicitly first')


def inspect_network():
    result = subprocess.run(['docker', 'network', 'inspect', NETWORK], text=True, capture_output=True)
    if result.returncode:
        # Distinguish absent network from a broken Docker daemon.
        networks = run('docker', 'network', 'ls', '--format', '{{.Name}}').splitlines()
        if NETWORK in networks:
            raise RuntimeError(result.stderr.strip())
        return None
    return json.loads(result.stdout)[0]


def public_ca():
    port = int(os.environ.get('LAB_BAO_PORT', '8200'))
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/v1/ssh-client-signer/public_key', timeout=5) as response:
        value = response.read().decode('ascii').strip()
    if not value.startswith(('ssh-', 'ecdsa-')):
        raise RuntimeError('OpenBao public CA response is invalid')
    return value


def protected_json(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
    path.chmod(0o600)


def bao_volume(containers):
    for container in containers:
        if container['Config']['Labels']['com.docker.compose.service'] == 'openbao':
            for mount in container.get('Mounts', []):
                if mount.get('Destination') == '/opt/latch/bao' and mount.get('Type') == 'volume':
                    return mount['Name']
    raise RuntimeError('OpenBao persistent volume could not be identified; migration aborted')


def verify():
    if not PENDING.exists():
        return
    record = json.loads(PENDING.read_text())
    network = inspect_network()
    if not network or {item.get('Subnet') for item in network['IPAM']['Config']} != {NEW}:
        raise RuntimeError('Management network has not been recreated on ' + NEW)
    containers = [json.loads(run('docker', 'inspect', identifier))[0]
                  for identifier in network.get('Containers', {})]
    if bao_volume(containers) != record['volume']:
        raise RuntimeError('OpenBao volume changed during migration; stop and restore original volume')
    if public_ca() != record['public_ca']:
        raise RuntimeError('OpenBao CA changed during migration; stop and restore the cold backup')
    protected_json(Path(record['backup']) / 'verified.json', {'network': NEW, 'volume': record['volume']})
    PENDING.unlink()
    print('Management migration verified: original OpenBao volume and public CA preserved')


def prepare():
    if PENDING.exists():
        print('Management migration already prepared; recreate services, then run verify')
        return
    network = inspect_network()
    if network is None:
        check_routes(json.loads(run('ip', '-j', '-4', 'route', 'show', 'table', 'all')))
        return
    subnets = {item.get('Subnet') for item in network['IPAM']['Config']}
    if subnets == {NEW}:
        if network['IPAM']['Config'][0].get('Gateway') != '10.1.3.254':
            raise RuntimeError('Management network uses an unexpected gateway')
        return
    if subnets != {OLD}:
        raise RuntimeError('Unexpected management subnet; refusing automatic migration')
    check_routes(json.loads(run('ip', '-j', '-4', 'route', 'show', 'table', 'all')))
    containers = [json.loads(run('docker', 'inspect', identifier))[0]
                  for identifier in network.get('Containers', {})]
    for container in containers:
        check_container(container)
    volume = bao_volume(containers)
    ca = public_ca()
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    ROOT.chmod(0o700)
    backup = ROOT / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    backup.mkdir(mode=0o700)
    protected_json(backup / 'network.json', network)
    protected_json(backup / 'containers.json', containers)
    config = json.loads(run('docker', 'compose', '-f', str(BASE / 'compose.lima.yaml'), 'config', '--format', 'json', cwd=BASE))
    # A running container can reference a removed image ID after Compose rebuilds
    # its tag. Resolve the newly built local image before stopping the service.
    image = config['services']['openbao'].get('image', 'latch-poc-openbao:latest')
    run('docker', 'image', 'inspect', image)
    key_names = {'Subnet': 'subnet', 'Gateway': 'gateway', 'IPRange': 'ip_range',
                 'AuxiliaryAddresses': 'aux_addresses'}
    config['networks']['management']['ipam']['config'] = [
        {key_names[key]: value for key, value in item.items() if key in key_names}
        for item in network['IPAM']['Config']]
    protected_json(backup / 'rollback-compose.json', config)
    record = {'backup': str(backup), 'volume': volume, 'public_ca': ca}
    stopped = []
    try:
        for container in containers:
            if container.get('State', {}).get('Running'):
                run('docker', 'stop', container['Id'])
                stopped.append(container['Id'])
        archive = backup / 'openbao-volume.tar'
        with archive.open('wb') as stream:
            archive.chmod(0o600)
            subprocess.run(['docker', 'run', '--rm', '--pull=never', '--network', 'none', '--user', '0',
                            '--entrypoint', 'tar', '-v', f'{volume}:/backup:ro', image,
                            '-C', '/backup', '-cf', '-', '.'], stdout=stream, check=True)
        protected_json(PENDING, record)
        for container in containers:
            run('docker', 'rm', container['Id'])  # Deliberately never delete volumes.
        run('docker', 'network', 'rm', NETWORK)
    except Exception:
        for identifier in stopped:
            subprocess.run(['docker', 'start', identifier], capture_output=True)
        print(f'Migration failed. Protected backup: {backup}. To restore the old network/services: '
              f'docker compose -f {backup}/rollback-compose.json up -d openbao latch-api. '
              'Keep the original OpenBao volume; never run compose down -v.', file=sys.stderr)
        raise
    print(f'Management migration prepared; protected cold backup: {backup}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['prepare', 'verify'])
    args = parser.parse_args()
    try:
        {'prepare': prepare, 'verify': verify}[args.mode]()
    except Exception as error:
        print(f'Management migration: {error}', file=sys.stderr)
        if PENDING.exists():
            record = json.loads(PENDING.read_text())
            print(f'Rollback configuration: {record["backup"]}/rollback-compose.json', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
