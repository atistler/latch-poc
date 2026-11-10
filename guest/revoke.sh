#!/bin/bash
source /home/stack/latch/guest/common.sh
if [[ -f "$BASE/config/backend.env" ]] && grep -Fxq 'LAB_BACKEND=lima' "$BASE/config/backend.env"; then
    echo 'Use the Latch server API through ./server-grant or ./server-revoke.' >&2
    exit 1
fi
os_auth
fip_id=$(openstack floating ip list --floating-ip-address "$FIP" -f value -c ID)
[[ -n "$fip_id" ]] || { echo 'Run ./lab provision first'; exit 1; }
# Close the entry point first, then cancel established Warpgate sessions.
forwardings=$(openstack floating ip port forwarding list "$fip_id" -f value -c ID)
while read -r id; do
    [[ -z "$id" ]] || openstack floating ip port forwarding delete "$fip_id" "$id"
done <<< "$forwardings"
echo 'Neutron forwarding removed; laptop CLI will cancel Warpgate sessions.'
