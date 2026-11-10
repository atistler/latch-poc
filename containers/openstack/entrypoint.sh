#!/bin/bash
set -euo pipefail
cd /state
for value in "${LAB_MEMORY:-6144}" "${LAB_CPUS:-4}"; do
    [[ "$value" =~ ^[1-9][0-9]*$ ]] || { echo 'Invalid CPU/memory setting' >&2; exit 1; }
done
[[ "${LAB_DISK:-64G}" =~ ^[1-9][0-9]*[GM]$ ]] || exit 1
if [[ ! -f base.img ]]; then
    url=https://cloud-images.ubuntu.com/noble/current
    curl --fail --location --retry 5 "$url/SHA256SUMS" -o SHA256SUMS
    curl --fail --location --retry 5 "$url/noble-server-cloudimg-amd64.img" -o base.img.part
    expected=$(awk '$2 ~ /noble-server-cloudimg-amd64.img$/ {print $1}' SHA256SUMS)
    [[ "$expected" =~ ^[a-f0-9]{64}$ ]] || { echo 'Missing image checksum' >&2; exit 1; }
    printf '%s  base.img.part\n' "$expected" | sha256sum --check -
    mv base.img.part base.img
fi
if [[ ! -f disk.qcow2 ]]; then
    qemu-img create -f qcow2 -F qcow2 -b /state/base.img disk.qcow2 "${LAB_DISK:-64G}"
fi
cloud-localds seed.iso user-data meta-data
# Bridge the dedicated Docker provider interface to QEMU's second NIC.
provider_if=$(ip -o -4 addr show | awk '$4 == "100.64.10.2/24" {print $2}')
[[ -n "$provider_if" ]] || { echo 'Missing provider interface' >&2; exit 1; }
ip link add br-provider type bridge
ip link set br-provider up
ip addr del 100.64.10.2/24 dev "$provider_if"
ip link set "$provider_if" master br-provider
ip addr add 100.64.10.2/24 dev br-provider
ip tuntap add dev provider-tap mode tap
ip link set provider-tap master br-provider
ip link set provider-tap up
# TCG works on Apple Silicon without exposing /dev/kvm or Docker's host kernel.
exec qemu-system-x86_64 \
    -machine q35,accel=tcg -cpu max -smp "${LAB_CPUS:-4}" -m "${LAB_MEMORY:-6144}" \
    -drive file=/state/disk.qcow2,if=virtio,format=qcow2 \
    -drive file=/state/seed.iso,if=virtio,format=raw,readonly=on \
    -netdev user,id=mgmt,net=172.20.2.0/24,host=172.20.2.2,dns=172.20.2.3,dhcpstart=172.20.2.15,hostfwd=tcp::2222-:22 \
    -device virtio-net-pci,netdev=mgmt,mac=52:54:00:12:34:56 \
    -netdev tap,id=provider,ifname=provider-tap,script=no,downscript=no \
    -device virtio-net-pci,netdev=provider,mac=52:54:00:12:34:57 \
    -display none -serial mon:stdio
