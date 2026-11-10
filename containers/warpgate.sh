#!/bin/bash
set -euo pipefail
umask 077
# Keep connected ingress, OpenBao management and provider routes. The
# gateway must not offer an alternate route into the tenant network.
while ip route del default 2>/dev/null; do :; done
# Reach device management through the lab router, never by joining its subnet.
# Legacy deployments leave these unset and retain their connected routes only.
if [[ -n ${LATCH_DEVICE_ROUTE:-} ]]; then
    ip route replace "$LATCH_DEVICE_ROUTE" via "${LATCH_DEVICE_NEXT_HOP:?Missing device next hop}"
fi
# Warpgate otherwise resolves its cluster address by connecting a UDP socket to
# 1.1.1.1. Supply the connected control address so startup needs no default route.
export WARPGATE_PEER_ADDRESS=10.1.1.2:8888
# An interrupted first setup can leave YAML before installing TLS material.
# Preserve that partial file and rerun setup against the existing database.
if [[ -f /config/warpgate.yaml && ! -f /data/tls.certificate.pem ]]; then
    mv /config/warpgate.yaml "/config/warpgate.yaml.incomplete.$(date +%s)"
fi
if [[ ! -f /config/warpgate.yaml ]]; then
    [[ -s /config/admin-password ]] || {
        echo 'Missing Warpgate bootstrap password. Run ./lab up to prepare it.' >&2
        exit 1
    }
    WARPGATE_ADMIN_PASSWORD=$(cat /config/admin-password)
    export WARPGATE_ADMIN_PASSWORD
    warpgate --config /config/warpgate.yaml unattended-setup \
        --data-path /data --http-port 8888 --ssh-port 2222 \
        --record-sessions --host-key-verification auto-reject \
        --external-host localhost --listen-address 0.0.0.0 \
        --if-config-exists skip
    unset WARPGATE_ADMIN_PASSWORD
fi
exec warpgate --config /config/warpgate.yaml run
