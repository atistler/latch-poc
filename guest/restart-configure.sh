#!/bin/bash
# Configure the units actually installed by this DevStack release.
source /home/stack/latch/guest/common.sh

resolve_unit() {
    local candidate
    for candidate in "$@"; do
        if [[ "$(systemctl show "$candidate" --property=LoadState --value)" == loaded ]]; then
            printf '%s\n' "$candidate"
            return
        fi
    done
    echo "Missing required service (tried: $*)" >&2
    return 1
}

database=$(resolve_unit mariadb.service mysql.service)
rabbit=$(resolve_unit rabbitmq-server.service)
ovs=$(resolve_unit openvswitch-switch.service)
systemctl is-enabled latch-management.service >/dev/null

# Database and broker can bind the management address too. Order them before
# DevStack, and give ordinary DevStack units the retry behavior UWSGI has.
for unit in "$database" "$rabbit"; do
    mkdir -p "/etc/systemd/system/$unit.d"
    cat > "/etc/systemd/system/$unit.d/latch-boot.conf" <<'EOF'
[Unit]
Requires=latch-management.service
After=latch-management.service
EOF
done
services=()
while read -r unit _; do services+=("$unit"); done < <(
    systemctl list-unit-files 'devstack@*.service' --type=service --no-legend)
[[ ${#services[@]} -gt 0 ]] || { echo 'No installed DevStack service units' >&2; exit 1; }
for unit in "${services[@]}"; do
    mkdir -p "/etc/systemd/system/$unit.d"
    cat > "/etc/systemd/system/$unit.d/latch-boot.conf" <<EOF
[Unit]
Requires=latch-management.service
Wants=$database $rabbit $ovs network-online.target
After=latch-management.service $database $rabbit $ovs network-online.target
StartLimitIntervalSec=0
[Service]
Restart=on-failure
RestartSec=5
EOF
    if [[ "$unit" == devstack@q-agt.service ]]; then
        # OVS can hang while draining RPC at shutdown. DevStack's five-minute
        # stop timeout exceeds resume.sh's bounded recovery restart budget.
        printf 'TimeoutStopSec=30\n' >> "/etc/systemd/system/$unit.d/latch-boot.conf"
    fi
done
systemctl daemon-reload
