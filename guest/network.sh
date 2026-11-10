#!/bin/bash
set -euo pipefail
if [[ -f /home/stack/latch/config/backend.env ]]; then
    source /home/stack/latch/config/backend.env
fi
if [[ "${LAB_BACKEND:-legacy}" == lima ]]; then
    # Docker owns the IPAM/gateway on latch-provider; veth supplies an L2
    # uplink into OVS without granting the worker a tenant route.
    ip link show latch-provider >/dev/null
    if ! ip link show latch-ovs >/dev/null 2>&1; then
        ip link add latch-ovs type veth peer name latch-docker
    fi
    ip link set latch-docker master latch-provider
    ip link set latch-docker up
    ip link set latch-ovs up
    ovs-vsctl --may-exist add-br br-ex
    ip link set br-ex up
    # /32 avoids competing with Docker's connected provider /24 route.
    ip addr replace 100.64.10.1/32 dev br-ex
    ovs-vsctl --may-exist add-port br-ex latch-ovs
    # Docker's bridge isolation can drop forwarded frames between an
    # unmanaged veth and a published container. Permit only this lab bridge.
    iptables -C DOCKER-USER -i latch-provider -o latch-provider -j ACCEPT 2>/dev/null ||
        iptables -I DOCKER-USER 1 -i latch-provider -o latch-provider -j ACCEPT
    exit 0
fi
# Stable MAC identifies QEMU's provider NIC regardless of kernel naming.
provider_if=
for nic in /sys/class/net/*; do
    if [[ "$(cat "$nic/address")" == 52:54:00:12:34:57 ]]; then
        provider_if=${nic##*/}
    fi
done
[[ -n "$provider_if" ]] || { echo 'Missing QEMU provider NIC; rerun ./lab up' >&2; exit 1; }
ovs-vsctl --may-exist add-br br-ex
ip link set br-ex up
ip addr replace 100.64.10.1/24 dev br-ex
ip link set "$provider_if" up
ovs-vsctl --may-exist add-port br-ex "$provider_if"
