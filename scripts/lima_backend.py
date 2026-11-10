"""ARM64 Linux VM lifecycle; all Docker operations stay inside this VM."""
import json
import platform
import re
import shutil
import subprocess

NAME = 'latch-arm64'
REMOTE = '/home/stack/latch'
PORTS = {'LAB_API_PORT': '9200', 'LAB_WORKER_PORT': '9202', 'LAB_BAO_PORT': '8200'}


def acceleration_mode(options):
    requested = options.get('LAB_ACCEL', 'auto')
    if requested not in ('auto', 'kvm', 'tcg'):
        raise ValueError('LAB_ACCEL must be auto, kvm, or tcg')
    return requested


def configuration(options, chip, macos):
    match = re.search(r'Apple M(\d+)', chip)
    nested = bool(match and int(match[1]) >= 3 and int(macos.split('.')[0]) >= 15)
    accel = acceleration_mode(options)
    if accel == 'kvm' and not nested:
        raise ValueError('Nested KVM requires Apple M3 or later and macOS 15 or later')
    forwards = [{'guestPort': int(options.get(key, default)), 'hostPort': int(options.get(key, default)),
                 'hostIP': '127.0.0.1'} for key, default in PORTS.items()]
    # Match every guest bind address before Lima's implicit localhost fallback.
    forwards.append({'guestIP': '0.0.0.0', 'guestIPMustBeZero': False,
                     'proto': 'any', 'guestPortRange': [1, 65535], 'ignore': True})
    memory = options.get('LAB_MEMORY', '8192')
    cpus = options.get('LAB_CPUS', '6')
    disk = options.get('LAB_DISK', '80G')
    if not str(memory).isdigit() or not 4096 <= int(memory) <= 131072:
        raise ValueError('LAB_MEMORY must be 4096–131072 MiB')
    if not str(cpus).isdigit() or not 2 <= int(cpus) <= 64:
        raise ValueError('LAB_CPUS must be 2–64')
    if not re.fullmatch(r'[1-9][0-9]*G', disk):
        raise ValueError('LAB_DISK must be a whole number of GB, for example 80G')
    return {
        'vmType': 'vz', 'arch': 'aarch64',
        'nestedVirtualization': nested and accel != 'tcg',
        'cpus': int(cpus), 'memory': f'{memory}MiB', 'disk': f'{disk[:-1]}GiB',
        'images': [{'location': 'https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-arm64.img',
                    'arch': 'aarch64'}],
        'mounts': [], 'containerd': {'system': False, 'user': False},
        'portForwards': forwards,
        'provision': [{'mode': 'system', 'script': '''#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y docker.io docker-compose-v2 curl jq git netcat-openbsd python3-venv
systemctl enable --now docker
id stack >/dev/null 2>&1 || useradd -m -s /bin/bash stack
printf 'stack ALL=(ALL) NOPASSWD:ALL\\n' > /etc/sudoers.d/latch-stack
chmod 440 /etc/sudoers.d/latch-stack
install -d -m 755 -o stack -g stack /home/stack/latch
# Preserve the lab's management address independently of Lima's NAT NIC.
cat > /etc/systemd/system/latch-management.service <<'EOF'
[Unit]
Description=Lab OpenStack management address
After=network.target
[Service]
Type=oneshot
ExecStart=/bin/bash -c 'ip link show latch-mgmt >/dev/null 2>&1 || ip link add latch-mgmt type dummy; ip addr replace 172.20.2.15/24 dev latch-mgmt; ip link set latch-mgmt up'
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now latch-management
'''}],
    }


def shell(*args):
    return ['limactl', 'shell', NAME, '--', *args]


def compose_command(args, options):
    # Neither Mac environment nor Docker Desktop's credentials/socket apply.
    env = ['sudo', 'env', '-u', 'DOCKER_HOST', '-u', 'DOCKER_CONTEXT',
           '-u', 'DOCKER_DEFAULT_PLATFORM', '-u', 'DOCKER_CONFIG']
    env += [f'{key}={value}' for key, value in options.items() if key in PORTS]
    return shell(*env, 'docker', 'compose', '--project-directory', REMOTE,
                 '-f', f'{REMOTE}/compose.lima.yaml', *args)


def start(root, state, options):
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise RuntimeError('The Lima backend requires an Apple Silicon Mac; use LAB_BACKEND=legacy otherwise')
    if not shutil.which('limactl'):
        raise RuntimeError('Install Lima first: brew install lima')
    chip = subprocess.check_output(['sysctl', '-n', 'machdep.cpu.brand_string'], text=True).strip()
    config = configuration(options, chip, platform.mac_ver()[0])
    path = state / 'lima.yaml'
    existing = subprocess.run(['limactl', 'list', '--json'], capture_output=True, text=True, check=True)
    instances = [json.loads(line) for line in existing.stdout.splitlines() if line.strip()]
    if not any(instance['name'] == NAME for instance in instances):
        path.write_text(json.dumps(config, indent=2) + '\n')
        subprocess.run(['limactl', 'start', '--name', NAME, '--tty=false', str(path)], check=True, cwd=root)
    else:
        # VM CPU/memory/nested settings are creation-time choices. Never silently
        # claim a changed .env was applied to an existing VM.
        if not path.exists():
            raise RuntimeError('Existing latch-arm64 VM is not registered in this workspace; refusing to attach it')
        if json.loads(path.read_text()) != config:
            raise RuntimeError('Lima settings changed. Restore the prior .env or explicitly migrate/recreate latch-arm64; its disk is retained.')
        subprocess.run(['limactl', 'start', NAME, '--tty=false'], check=True, cwd=root)
    print(f'Lima ARM64 host ready; requested tenant acceleration: {options.get("LAB_ACCEL", "auto")}', flush=True)


def sync(root, state, options):
    subprocess.run(shell('sudo', 'mkdir', '-p', REMOTE), check=True)
    sources = ['guest', 'config', 'containers', 'latch_api', 'latch_common',
               'compose.lima.yaml']
    producer = subprocess.Popen(['tar', '--no-xattrs', '-cf', '-', *sources], cwd=root, stdout=subprocess.PIPE)
    try:
        subprocess.run(shell('sudo', 'tar', '-xf', '-', '-C', REMOTE), stdin=producer.stdout, check=True)
    finally:
        producer.stdout.close()
        if producer.wait():
            raise RuntimeError('Could not package Lima sources')
    subprocess.run(shell('sudo', 'mkdir', '-p', f'{REMOTE}/.state/warpgate'), check=True)
    subprocess.run(shell('sudo', 'chmod', '700', f'{REMOTE}/.state/warpgate'), check=True)
    files = [('warpgate/admin-password', 'admin-password'), ('vm_known_hosts', 'vm_known_hosts')]
    for source, destination in files:
        if not (state / source).exists():
            continue
        remote = f'{REMOTE}/.state/warpgate/{destination}'
        subprocess.run(shell('sudo', 'tee', remote), input=(state / source).read_bytes(),
                       stdout=subprocess.DEVNULL, check=True)
        subprocess.run(shell('sudo', 'chmod', '600', remote), check=True)
    subprocess.run(shell('sudo', 'chown', '-R', 'stack:stack', REMOTE), check=True)
    accel = acceleration_mode(options)
    subprocess.run(shell('sudo', 'tee', f'{REMOTE}/config/backend.env'),
                   input=f'LAB_BACKEND=lima\nLAB_ACCEL={accel}\n'.encode(), stdout=subprocess.DEVNULL, check=True)
