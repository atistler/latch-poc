#!/usr/bin/env python3
"""Local Warpgate/OpenStack lab: Lima ARM64 or the legacy Docker runner."""
import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import urllib.request
from uuid import UUID
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from certificates import BaoError, issue
import lima_backend as lima
import warpgate
import device_integration
import server_integration

PORT_DEFAULTS = {'LAB_SSH_PORT': '2222', **lima.PORTS}


def backend():
    value = settings().get('LAB_BACKEND', 'lima')
    if value not in ('lima', 'legacy'):
        raise ValueError('LAB_BACKEND must be lima or legacy')
    return value


def run(args, **kwargs):
    if args[:2] == ['docker', 'compose'] and backend() == 'lima':
        args = lima.compose_command(args[2:], settings())
    elif args[:2] == ['docker', 'compose']:
        args = [*args[:2], '-f', str(ROOT / 'compose.legacy.yaml'), *args[2:]]
        if 'env' not in kwargs:
            env = os.environ.copy()
            # A globally configured amd64 default would emulate the QEMU runner too.
            env.pop('DOCKER_DEFAULT_PLATFORM', None)
            kwargs['env'] = env
    kwargs.setdefault("check", True)
    return subprocess.run(args, cwd=ROOT, **kwargs)


def settings():
    result = {}
    path = ROOT / '.env'
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                key, value = line.split('=', 1)
                result[key.strip()] = value.strip()
    for key, default in PORT_DEFAULTS.items():
        result[key] = os.environ.get(key, result.get(key, default))
        if not result[key].isdigit() or not 1024 <= int(result[key]) <= 65535:
            raise ValueError(f'{key} must be a port from 1024 to 65535')
    if len({result[key] for key in PORT_DEFAULTS}) != len(PORT_DEFAULTS):
        raise ValueError('Host SSH, Warpgate HTTPS, gateway SSH and OpenBao ports must be different')
    for key in ('LAB_BACKEND', 'LAB_ACCEL', 'LAB_MEMORY', 'LAB_CPUS', 'LAB_DISK'):
        if key in os.environ:
            result[key] = os.environ[key]
    return result


STATE = ROOT / '.state' / 'lima' if backend() == 'lima' else ROOT / '.state'


def ssh_args():
    if backend() == 'lima':
        return lima.shell('sudo', '-H', '-u', 'stack', 'bash', '-c')
    return ['ssh', '-i', str(STATE / 'host_ed25519'), '-p', settings()['LAB_SSH_PORT'],
            '-o', 'StrictHostKeyChecking=accept-new', '-o', f'UserKnownHostsFile={STATE / "known_hosts"}',
            '-o', 'ConnectTimeout=10', '-o', 'BatchMode=yes', 'stack@127.0.0.1']


def guest(command, **kwargs):
    return run(ssh_args() + [command], **kwargs)


def prepare_legacy_guest():
    """Create the SSH identity and cloud-init seed consumed by the QEMU runner."""
    key = STATE / 'host_ed25519'
    if not key.exists():
        run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)])
    public = key.with_suffix('.pub').read_text().strip()
    (STATE / 'user-data').write_text('''#cloud-config
hostname: latch-openstack
users:
  - name: stack
    groups: [sudo]
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    lock_passwd: true
    ssh_authorized_keys:
      - ''' + public + '''
ssh_pwauth: false
disable_root: true
growpart:
  mode: auto
  devices: ['/']
resize_rootfs: true
''')
    (STATE / 'meta-data').write_text('instance-id: latch-openstack-001\nlocal-hostname: latch-openstack\n')


def prepare():
    STATE.mkdir(mode=0o700, exist_ok=True)
    STATE.chmod(0o700)
    if backend() == 'legacy':
        prepare_legacy_guest()
    warpgate.prepare(STATE)
    known = STATE / 'vm_known_hosts'
    if backend() == 'legacy' and known.exists():
        (STATE / 'warpgate/vm_known_hosts').write_bytes(known.read_bytes())


def sync():
    if backend() == 'lima':
        lima.sync(ROOT, STATE, settings())
        return
    guest('mkdir -p /home/stack/latch')
    producer = subprocess.Popen(['tar', '-cf', '-', 'guest', 'config', 'latch_common'], cwd=ROOT, stdout=subprocess.PIPE)
    try:
        run(ssh_args() + ['tar -xf - -C /home/stack/latch'], stdin=producer.stdout)
    finally:
        producer.stdout.close()
        if producer.wait() != 0:
            raise RuntimeError('Could not package guest sources')


def wait_ssh():
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        try:
            guest('true', stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                guest('sudo cloud-init status --wait')
            except subprocess.CalledProcessError as error:
                if error.returncode != 2:
                    raise
                print('Cloud-init reported recoverable warnings; inspect cloud-init logs in the guest.')
            return
        except subprocess.CalledProcessError:
            time.sleep(10)
    raise RuntimeError('Linux guest did not become ready in 30 minutes; inspect ./lab logs')


def bao_address():
    return f'http://127.0.0.1:{settings()["LAB_BAO_PORT"]}'


def gateway_api():
    return warpgate.Api(f'https://localhost:{settings()["LAB_API_PORT"]}', STATE)


def docker(*args, **kwargs):
    command = ['docker', *args]
    if backend() == 'lima':
        command = lima.shell('sudo', *command)
    return run(command, **kwargs)


def retire_boundary():
    """Keep stopped containers and volumes available for an explicit rollback."""
    for service in ('boundary-worker', 'boundary-controller', 'db'):
        name = f'latch-poc-{service}-1'
        exists = docker('container', 'inspect', name, check=False,
                        capture_output=True, text=True)
        if exists.returncode:
            continue
        docker('stop', name, stdout=subprocess.DEVNULL)
        # A stopped endpoint still reserves its IP. Detach the shared networks.
        container = json.loads(exists.stdout)[0]
        for network in container['NetworkSettings']['Networks']:
            if network in ('latch-poc_provider', 'latch-poc_control'):
                docker('network', 'disconnect', network, name, check=False,
                       capture_output=True, text=True)


def configure_gateway():
    """Configure a dedicated signer, pin TLS and the VM key, then authorize SSH."""
    result = worker('cat', '/data/tls.certificate.pem', capture_output=True)
    pin = STATE / 'warpgate/tls.certificate.pem'
    if pin.exists() and pin.read_bytes() != result.stdout:
        raise RuntimeError('Warpgate TLS certificate changed; review the gateway identity before replacing its pin')
    pin.write_bytes(result.stdout)
    # Read AppRole credentials inside OpenBao. Neither root token nor secret ID
    # is put on a command line or printed to the terminal.
    result = run(['docker', 'compose', 'exec', '-T', 'openbao', 'bash', '-c',
                  'export BAO_ADDR=http://127.0.0.1:8200; '
                  'export BAO_TOKEN=$(jq -er .root_token /opt/latch/bao/init.json); '
                  'bao read -format=json auth/approle/role/latch-warpgate/role-id; '
                  'bao write -format=json -f auth/approle/role/latch-warpgate/secret-id'],
                 capture_output=True, text=True)
    decoder = json.JSONDecoder()
    role, end = decoder.raw_decode(result.stdout.lstrip())
    secret = json.loads(result.stdout.lstrip()[end:].strip())
    host_key = None
    known = STATE / 'vm_known_hosts'
    if known.exists():
        for line in known.read_text().splitlines():
            fields = line.split()
            if len(fields) >= 3 and fields[0] == 'latch-poc-vm':
                host_key = ' '.join(fields[1:3])
                break
    if host_key is None:
        # Cloud-init prints public host keys on the OpenStack console. Obtain
        # the initial trust anchor through the host, rather than network TOFU.
        result = guest('source /home/stack/latch/guest/common.sh; os_auth; '
                       'openstack console log show latch-vm', capture_output=True, text=True)
        import re
        match = re.search(r'ssh-ed25519 [A-Za-z0-9+/=]+', result.stdout)
        if not match:
            raise RuntimeError('VM public host key not ready on console; retry ./lab provision after cloud-init')
        host_key = match.group()
        with known.open('a') as stream:
            stream.write(f'latch-poc-vm {host_key}\n')
        sync()
    warpgate.bootstrap(gateway_api(), STATE, role_id=role['data']['role_id'],
                      secret_id=secret['data']['secret_id'], host='100.64.10.120',
                      port=22001, host_public_key=host_key, isolated=backend() == 'lima')
    if backend() == 'lima':
        server_integration.up(ROOT, STATE, settings(), host_key, gateway_api())
    print('Warpgate configured with a pinned VM host key and OpenBao certificates.')


def worker(*args, **kwargs):
    return run(['docker', 'compose', 'exec', '-T', 'warpgate', *args], **kwargs)


def verify_worker():
    result = worker('ip', 'route', 'get', '192.168.20.15', check=False,
                    capture_output=True, text=True)
    if result.returncode == 0 or 'Network is unreachable' not in result.stderr:
        raise RuntimeError('Gateway isolation check did not confirm an unreachable tenant route')
    print('PASS: gateway has no direct tenant route')
    deadline = time.monotonic() + 360
    while True:
        try:
            worker('nc', '-z', '-w', '2', '100.64.10.120', '22001',
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            break
        except subprocess.CalledProcessError:
            if time.monotonic() >= deadline:
                raise RuntimeError('Gateway cannot reach Neutron floating-IP forwarding endpoint')
            time.sleep(2)
    print('PASS: gateway reaches VM through Neutron port forwarding')
    # Probe from a provider peer on the worker's ingress path: a temporary
    # container in Lima, or the runner in legacy. The host's br-ex is not equivalent.
    probe = (['run', '--rm', '--no-deps', 'provider-probe'] if backend() == 'lima'
             else ['exec', '-T', 'openstack', 'nc'])
    denied = run(['docker', 'compose', *probe,
                  '-z', '-s', '100.64.10.2', '-w', '5', '100.64.10.120', '22001'],
                 check=False, capture_output=True, text=True)
    if denied.returncode != 1:
        raise RuntimeError('Provider source outside gateway /32 was not denied: ' + denied.stderr)
    print('PASS: provider source outside gateway /32 denied')


def connect(interactive=True, remote_command=None, grant_id=None):
    if backend() == 'lima':
        if not grant_id:
            raise RuntimeError('server-connect requires --grant GRANT_UUID; issue a grant with ./server-grant first')
        grant = server_integration.request(settings(), 'GET', '/v1/server-grants/' + str(UUID(grant_id)))
        command = server_integration.ssh_command(
            STATE, settings(), grant, tty=interactive,
            command=None if interactive else (remote_command or 'hostname; echo "$SSH_CONNECTION"'))
    else:
        command = warpgate.ssh_command(STATE, settings(),
                                      command=None if interactive else (remote_command or 'hostname; echo "$SSH_CONNECTION"'),
                                      tty=interactive)
    if interactive:
        run(command)
        return
    proof = run(command, capture_output=True, text=True, timeout=90)
    print(proof.stdout)
    if not any(line.startswith('100.64.10.10 ') for line in proof.stdout.splitlines()):
        raise RuntimeError('Server did not observe gateway source IP in SSH connection')
    print('PASS: laptop -> Warpgate SSH target -> Neutron L3 forwarding -> OpenStack server')
    return proof.stdout


def certificate_probe(credentials, username='ubuntu'):
    """Check VM certificate policy from the gateway's authorized provider IP."""
    key, certificate = credentials
    payload = {'key': Path(key).read_text(),
               'certificate': Path(certificate).read_text() if certificate else None,
               'username': username}
    script = r'''import json, os, pathlib, subprocess, sys, tempfile
p = json.load(sys.stdin)
with tempfile.TemporaryDirectory(prefix="latch-cert-") as directory:
    key = pathlib.Path(directory) / "key"
    key.write_text(p["key"]); key.chmod(0o600)
    cert = pathlib.Path(directory) / "cert.pub"
    if p["certificate"]: cert.write_text(p["certificate"])
    args = ["ssh", "-i", str(key), "-o", "CertificateFile=" + (str(cert) if p["certificate"] else "none"),
            "-o", "IdentityAgent=none", "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
            "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=/config/vm_known_hosts",
            "-o", "HostKeyAlias=latch-poc-vm", "-o", "ConnectTimeout=15",
            "-p", "22001", p["username"] + "@100.64.10.120", "true"]
    sys.exit(subprocess.run(args).returncode)
'''
    return worker('python3', '-c', script, input=json.dumps(payload),
                  capture_output=True, text=True, check=False, timeout=30)


def verify_certificates(grant_id=None):
    address = bao_address()
    credentials = issue(address, STATE)
    # Wait for Ubuntu cloud-init to install CA trust and restart SSH.
    deadline = time.monotonic() + 1800
    while True:
        try:
            connect(interactive=False, grant_id=grant_id)
            break
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            if time.monotonic() >= deadline:
                raise RuntimeError('Certificate SSH did not become ready in 30 minutes; inspect target console/cloud-init')
            time.sleep(5)
            credentials = issue(address, STATE)
    for principal, ttl in [('root', '15m'), ('ubuntu', '1h')]:
        try:
            issue(address, STATE, ttl=ttl, principal=principal, name='rejected-cert.pub')
        except BaoError as error:
            if error.status != 400:
                raise
        else:
            raise RuntimeError('OpenBao allowed an unauthorized principal or excessive TTL')
    print('PASS: signing role rejects unauthorized principals and excessive TTL')
    valid = certificate_probe(credentials)
    if valid.returncode:
        raise RuntimeError('Valid certificate control failed: ' + valid.stderr)
    key, cert = credentials
    expired = issue(address, STATE, ttl='1s', name='expired-cert.pub')
    time.sleep(3)
    for label, pair, username in [('unsigned key', (key, None), 'ubuntu'),
                                  ('wrong principal', (key, cert), 'latch-other'),
                                  ('expired certificate', expired, 'ubuntu')]:
        try:
            result = certificate_probe(pair, username)
            if result.returncode == 0:
                raise RuntimeError(f'SSH unexpectedly accepted {label}')
            if 'Permission denied (publickey)' not in result.stderr:
                raise RuntimeError(f'{label}: failed without an explicit SSH authentication rejection')
            print(f'PASS: SSH rejects {label}')
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f'{label}: timed out instead of rejecting authentication') from error
    connect(interactive=False, grant_id=grant_id)
    marker = f'latch-recording-{time.time_ns()}'
    connect(interactive=False, remote_command=f'echo {marker}; echo "$SSH_CONNECTION"', grant_id=grant_id)
    for attempt in range(10):
        try:
            grant = server_integration.request(settings(), 'GET', '/v1/server-grants/' + grant_id) if grant_id else None
            filters = dict(zip(('username', 'target_name'), grant['ssh_username'].split(':', 1))) if grant else {}
            recording = warpgate.verify_recording(gateway_api(), marker, **filters)
            print('PASS: replayable SSH recording contains session output: ' + recording['recording_id'])
            break
        except RuntimeError:
            if attempt == 9:
                raise
            time.sleep(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['up', 'install', 'provision', 'status', 'logs', 'ssh', 'connect', 'certificate', 'verify', 'recordings', 'revoke', 'grant', 'down', 'doctor', 'device-grant', 'device-revoke', 'device-connect', 'network-up', 'network-verify', 'network-down', 'server-grant', 'server-status', 'server-revoke', 'server-connect'])
    parser.add_argument('--device', help='Operator-approved device inventory ID')
    parser.add_argument('--role', choices=['readonly', 'operator', 'admin'], default='readonly')
    parser.add_argument('--ttl', type=int, default=300)
    parser.add_argument('--grant', help='Existing server or device grant UUID')
    parser.add_argument('--server', '--instance', dest='server', help='Operator-approved server UUID (currently OpenStack inventory)')
    args = parser.parse_args()
    command = args.command
    if command in ('server-grant', 'server-status', 'server-revoke') or (backend() == 'lima' and command in ('grant', 'revoke')):
        if backend() != 'lima':
            raise RuntimeError('Server orchestration currently requires the Lima OpenStack backend')
        if command in ('server-grant', 'grant'):
            result = server_integration.issue(STATE, settings(), args.server, args.ttl)
            server_integration.remember(STATE, result)
        elif command in ('server-revoke', 'revoke'):
            result = server_integration.revoke(STATE, settings(), args.grant)
        else:
            result = (server_integration.request(settings(), 'GET', '/v1/server-grants/' + str(UUID(args.grant)))
                      if args.grant else server_integration.current(STATE, settings()))
        print(json.dumps(result, indent=2))
    elif command.startswith('network-'):
        if backend() != 'lima':
            raise RuntimeError('Optional network-device integration requires the Lima backend')
        import network_integration
        if command == 'network-up':
            sync()
            network_integration.up(ROOT, STATE, settings())
        elif command == 'network-verify':
            network_integration.verify(ROOT, STATE, settings())
        else:
            network_integration.down(ROOT, STATE, settings())
    elif command.startswith('device-'):
        if backend() != 'lima':
            raise RuntimeError('Network-device access requires the Lima backend')
        if command == 'device-grant':
            if not args.device:
                parser.error('device-grant requires --device')
            payload = {'device_id': args.device, 'role': args.role, 'ttl_seconds': args.ttl,
                       'public_key': (STATE / 'gateway_ed25519.pub').read_text().strip()}
            print(json.dumps(device_integration.request(settings(), 'POST', '/v1/device-grants', payload), indent=2))
        else:
            if not args.grant:
                parser.error(command + ' requires --grant')
            grant_id = str(UUID(args.grant))
            method = 'DELETE' if command == 'device-revoke' else 'GET'
            result = device_integration.request(settings(), method, '/v1/device-grants/' + grant_id)
            if command == 'device-revoke':
                print(json.dumps(result, indent=2))
            else:
                if result['state'] != 'active':
                    raise RuntimeError('Device grant is not active')
                ssh = warpgate.ssh_command(STATE, settings(), tty=True)
                ssh[-1] = result['ssh_username'] + '@127.0.0.1'
                run(ssh)
    elif command == 'up':
        prepare()
        if backend() == 'lima':
            lima.start(ROOT, STATE, settings())
            sync()
            # Build before stopping services so a registry failure leaves them running.
            run(['docker', 'compose', 'build'])
            if (STATE / 'vm/inventory.json').exists():
                run(server_integration.compose(['build', 'server-api'], settings()))
            guest(f'sudo env LAB_BAO_PORT={settings().get("LAB_BAO_PORT", "8200")} python3 /home/stack/latch/guest/management-migrate.py prepare')
            retire_boundary()
            run(['docker', 'compose', 'up', '--no-build', '-d', '--wait', '--wait-timeout', '300'])
            guest(f'sudo env LAB_BAO_PORT={settings().get("LAB_BAO_PORT", "8200")} python3 /home/stack/latch/guest/management-migrate.py verify')
        else:
            run(['docker', 'compose', 'build'])
            retire_boundary()
            run(['docker', 'compose', 'up', '--no-build', '-d', '--wait', '--wait-timeout', '300'])
        if backend() == 'legacy':
            wait_ssh()
            sync()
        elif guest('test -f /opt/latch/install-complete', check=False).returncode == 0:
            guest('sudo systemctl restart latch-network')
            guest('sudo /bin/bash /home/stack/latch/guest/resume.sh')
            if (STATE / 'vm/inventory.json').exists():
                server_integration.retire_legacy_api(settings())
                run(server_integration.compose(['up', '-d', '--no-deps', '--wait', 'server-api'], settings()))
        print('Linux guest ready. Run ./lab provision to configure the Warpgate target, or ./lab install for a new host.')
    elif command == 'install':
        sync()
        guest('sudo systemd-run --unit=latch-install --collect /bin/bash /home/stack/latch/guest/install.sh')
        print('Installation started. Follow ./lab logs; run ./lab status to check completion.')
    elif command in ('provision', 'verify', 'revoke', 'grant'):
        if command == 'provision' and backend() != 'lima':
            raise RuntimeError('Dynamic vendor data requires the Lima backend. The legacy runner has no Latch API service.')
        sync()
        guest(f'sudo /bin/bash /home/stack/latch/guest/{command}.sh')
        if command == 'provision':
            configure_gateway()
            print('Provisioned. Next: ./lab verify, then ./server-grant and ./server-connect --grant GRANT_UUID')
        elif command == 'verify':
            grant = server_integration.current(STATE, settings()) if backend() == 'lima' else None
            created = backend() == 'lima' and grant is None
            if created:
                grant = server_integration.issue(STATE, settings())
                server_integration.remember(STATE, grant)
            try:
                verify_worker()
                verify_certificates(grant_id=grant['grant_id'] if grant else None)
            finally:
                if created:
                    server_integration.revoke(STATE, settings(), grant['grant_id'])
        elif command == 'revoke':
            warpgate.close_sessions(gateway_api())
            result = worker('nc', '-z', '-w', '5', '100.64.10.120', '22001', check=False)
            if result.returncode == 0:
                raise RuntimeError('Floating-IP port forwarding is still reachable')
            print('Revoked: forwarding removed, Warpgate sessions closed, new TCP connection denied.')
    elif command == 'recordings':
        print(json.dumps(gateway_api().request('GET', '/sessions?limit=100' if backend() == 'lima' else '/sessions?username=latch&limit=100'), indent=2))
    elif command == 'status':
        run(['docker', 'compose', 'ps'])
        guest('sudo /bin/bash /home/stack/latch/guest/status.sh')
    elif command == 'logs':
        guest('sudo journalctl -u latch-install -n 100 -f')
    elif command == 'ssh':
        if backend() == 'lima':
            run(lima.shell('sudo', '-H', '-u', 'stack', 'bash', '-l'))
        else:
            run(ssh_args()[:-1] + ['-t', ssh_args()[-1]])
    elif command in ('server-connect', 'connect'):
        if not args.grant:
            parser.error('server-connect requires --grant GRANT_UUID; run ./server-grant first')
        if command == 'server-connect' and backend() != 'lima':
            raise RuntimeError('Server grants currently require the Lima OpenStack backend')
        connect(grant_id=args.grant)
    elif command == 'certificate':
        _, certificate = issue(bao_address(), STATE)
        run(['ssh-keygen', '-L', '-f', str(certificate)])
    elif command == 'down':
        if backend() == 'lima':
            run(device_integration.compose(['--profile', 'network', '--profile', 'servers', 'down'], settings()))
        else:
            run(['docker', 'compose', 'down'])
        if backend() == 'lima':
            run(['limactl', 'stop', lima.NAME])
        if backend() == 'lima':
            print('Stopped. OpenStack disk and service volumes remain in Lima; laptop keys remain in .state/lima/.')
        else:
            print('Stopped. Guest disk and keys remain in .state/; service data remains in Docker volumes.')
    elif command == 'doctor':
        if backend() == 'lima':
            run(['limactl', '--version'])
            if platform.system() != 'Darwin' or platform.machine() != 'arm64':
                raise RuntimeError('Lima backend requires an Apple Silicon Mac')
            chip = run(['sysctl', '-n', 'machdep.cpu.brand_string'], capture_output=True, text=True).stdout.strip()
            config = lima.configuration(settings(), chip, platform.mac_ver()[0])
            print(f'{chip}; ARM64 VZ host; nested virtualization requested: {config["nestedVirtualization"]}')
            run(['limactl', 'list'])
            return
        response = run(['docker', 'info', '--format', '{{json .}}'], capture_output=True, text=True)
        info = json.loads(response.stdout)
        print(f'Docker: {info["Architecture"]}; CPUs: {info["NCPU"]}; memory: {info["MemTotal"] / 2**30:.1f} GiB')
        if info['MemTotal'] < 7 * 2**30:
            raise RuntimeError('Allocate at least 8 GB to Docker Desktop; 12–16 GB is recommended.')
        run(['docker', 'compose', 'config', '--quiet'])
        run(['ssh', '-V'])


if __name__ == '__main__':
    try:
        main()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError, ValueError, OSError) as error:
        print(f'Error: {error}', file=sys.stderr)
        sys.exit(1)
