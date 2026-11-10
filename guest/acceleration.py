#!/usr/bin/env python3
"""Select acceleration only after KVM creates a real ARM virtual CPU."""
import fcntl
import json
import os
from pathlib import Path
import platform
import sys


def probe_kvm():
    # Linux KVM ARM ABI: CREATE_VM, ARM_PREFERRED_TARGET, CREATE_VCPU,
    # ARM_VCPU_INIT. Opening /dev/kvm alone cannot prove nested support.
    with open('/dev/kvm', 'rb', buffering=0) as device:
        if fcntl.ioctl(device, 0xAE00, 0) != 12:
            raise OSError('Unexpected KVM API version')
        vm = fcntl.ioctl(device, 0xAE01, 0)
        try:
            target = bytearray(32)
            fcntl.ioctl(vm, 0x8020AEAF, target, True)
            cpu = fcntl.ioctl(vm, 0xAE41, 0)
            try:
                fcntl.ioctl(cpu, 0x4020AEAE, target)
            finally:
                os.close(cpu)
        finally:
            os.close(vm)


def select(requested):
    if requested not in ('auto', 'kvm', 'tcg'):
        raise ValueError('Invalid LAB_ACCEL')
    reason = 'Software emulation explicitly requested'
    if requested != 'tcg':
        try:
            probe_kvm()
            return 'kvm', 'KVM ARM virtual CPU initialization succeeded'
        except OSError as error:
            reason = str(error)
            if requested == 'kvm':
                raise RuntimeError(f'KVM was requested but is unavailable: {reason}') from error
    return 'qemu', reason


def main():
    if platform.machine() != 'aarch64':
        raise RuntimeError('Lima OpenStack host must be aarch64')
    requested = sys.argv[1]
    virt, reason = select(requested)
    report_path = Path('/opt/latch/acceleration.json')
    if Path('/opt/latch/openstack-installed').exists() and report_path.exists():
        previous = json.loads(report_path.read_text())
        if previous['virt_type'] != virt:
            raise RuntimeError('Installed Nova uses ' + previous['virt_type'] +
                               '; changing accelerators requires an explicit Nova migration/rebuild')
    source = Path('/home/stack/latch/config/openstack/local.conf').read_text()
    mode = 'host-passthrough' if virt == 'kvm' else 'custom'
    source = source.replace('LIBVIRT_TYPE=qemu', f'LIBVIRT_TYPE={virt}')
    source = source.replace('LIBVIRT_CPU_MODE=none', f'LIBVIRT_CPU_MODE={mode}')
    source = source.replace('virt_type=qemu', f'virt_type={virt}')
    source = source.replace('cpu_mode=none', f'cpu_mode={mode}')
    if virt == 'qemu':
        source = source.replace('NOVA_CPU_ALLOCATION_RATIO=4', 'LIBVIRT_CPU_MODEL=max\nNOVA_CPU_ALLOCATION_RATIO=4')
        source += '\ncpu_models=max\n'
    Path('/opt/stack/devstack/local.conf').write_text(source)
    report = {'architecture': 'aarch64', 'requested': requested, 'virt_type': virt, 'reason': reason}
    report_path.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
