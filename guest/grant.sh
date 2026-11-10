#!/bin/bash
source /home/stack/latch/guest/common.sh
if [[ -f "$BASE/config/backend.env" ]] && grep -Fxq 'LAB_BACKEND=lima' "$BASE/config/backend.env"; then
    echo 'Use the Latch server API through ./server-grant or ./server-revoke.' >&2
    exit 1
fi
os_auth
fip_id=$(openstack floating ip list --floating-ip-address "$FIP" -f value -c ID)
[[ -n "$fip_id" ]] || { echo 'Run ./lab provision first'; exit 1; }
rules=$(openstack floating ip port forwarding list "$fip_id" --external-protocol-port 22001 --protocol tcp -f value -c ID)
if [[ -z "$rules" ]]; then
    port_id=$(openstack port show latch-vm-port -f value -c id)
    openstack floating ip port forwarding create "$fip_id" --protocol tcp \
        --external-protocol-port 22001 --internal-protocol-port 22 \
        --internal-ip-address "$TENANT" --port "$port_id"
fi
echo "Granted $FIP:22001 -> $TENANT:22; security group allows only $WORKER/32"
