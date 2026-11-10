#!/bin/bash
# Configure both config-drive (nova-compute) and HTTP metadata delivery.
set -euo pipefail
BASE=/home/stack/latch
# Do not replace working Nova configuration until the upstream public CA is ready.
curl --fail --silent --show-error --connect-timeout 2 --max-time 4 \
    http://127.0.0.1:8300/health/ready >/dev/null
# DevStack runs compute with its own --config-file; metadata WSGI reads nova.conf.
configs=(/etc/nova/nova.conf)
[[ ! -f /etc/nova/nova-cpu.conf ]] || configs+=(/etc/nova/nova-cpu.conf)
for config in "${configs[@]}"; do
    if python3 "$BASE/guest/vendor_data_config.py" "$config" \
            "/opt/latch/config-backup/$(basename "$config").before-dynamic-json"; then
        :
    else
        result=$?
        [[ "$result" == 10 ]] || exit "$result"
        touch /run/latch-vendor-data-restart
    fi
done
if [[ -f /run/latch-vendor-data-restart ]]; then
    systemctl restart devstack@n-cpu.service devstack@n-api-meta.service
    rm /run/latch-vendor-data-restart
fi
