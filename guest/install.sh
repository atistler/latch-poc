#!/bin/bash
set -euo pipefail
source /home/stack/latch/guest/common.sh
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y git curl jq netcat-openbsd python3-venv \
    linux-modules-extra-"$(uname -r)"
install -d -o stack -g stack /opt/stack
# Migrate older labs: services now belong to Compose, never the guest.
for unit in latch-controller latch-worker latch-proxy latch-bao; do
    if [[ -f "/etc/systemd/system/$unit.service" ]]; then
        systemctl disable --now "$unit"
    fi
done
if ip netns list | awk '{print $1}' | grep -qx latch-worker; then
    ovs-vsctl --if-exists del-port br-ex latch-prov
    ip netns delete latch-worker
    ip link delete latch-ctl 2>/dev/null || true
fi
if [[ ! -d /opt/stack/devstack/.git ]]; then
    sudo -u stack git clone --branch stable/2025.2 --depth 1 \
        https://opendev.org/openstack/devstack.git /opt/stack/devstack
fi
if [[ -f "$BASE/config/backend.env" ]]; then
    source "$BASE/config/backend.env"
fi
if [[ "${LAB_BACKEND:-legacy}" == lima ]]; then
    apt-get install -y qemu-system-arm qemu-efi-aarch64
    python3 "$BASE/guest/acceleration.py" "${LAB_ACCEL:-auto}"
    chown stack:stack /opt/stack/devstack/local.conf
else
    install -o stack -g stack "$BASE/config/openstack/local.conf" /opt/stack/devstack/local.conf
fi
if [[ ! -f "$DATA/openstack-installed" ]]; then
    sudo -H -u stack bash -c 'cd /opt/stack/devstack && ./stack.sh'
    touch "$DATA/openstack-installed"
fi

install -m 755 "$BASE/guest/network.sh" /usr/local/sbin/latch-network
cat > /etc/systemd/system/latch-network.service <<'EOF'
[Unit]
Description=Connect OpenStack provider bridge to Docker provider network
After=openvswitch-switch.service docker.service
Requires=openvswitch-switch.service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/latch-network
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now latch-network
systemctl restart latch-network
bash "$BASE/guest/time-configure.sh"
if [[ "${LAB_BACKEND:-legacy}" == lima ]]; then
    bash "$BASE/guest/restart-configure.sh"
fi
touch "$DATA/install-complete"
echo 'Installation complete. Next on your laptop: ./lab provision'
