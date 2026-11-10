#!/bin/bash
# Called by ./lab up after Docker and the provider uplink are restored.
source /home/stack/latch/guest/common.sh
bash "$BASE/guest/restart-configure.sh"
systemctl start latch-management.service
services=()
while read -r unit _; do services+=("$unit"); done < <(
    systemctl list-unit-files 'devstack@*.service' --type=service --no-legend)
systemctl reset-failed "${services[@]}"
# Queue startup together; retrying units may not be ready on the first attempt.
systemctl start --no-block "${services[@]}"
os_auth

ready=false
deadline=$((SECONDS + 600))
agent_grace=$((SECONDS + 90))
rpc_recovered=false
while ((SECONDS < deadline)); do
    api_ready=false
    if servers=$(timeout 10 openstack server list -f json 2>/dev/null) &&
       agents=$(timeout 10 openstack network agent list -f json 2>/dev/null) &&
       routers=$(timeout 10 openstack router list -f json 2>/dev/null) &&
       compute=$(timeout 10 openstack compute service list --service nova-compute -f json 2>/dev/null); then
        api_ready=true
    fi
    router_ready=false
    if [[ "$api_ready" == true ]]; then
        router_id=$(jq -r '.[] | select(.Name == "latch-router") | .ID' <<< "$routers")
        if [[ -z "$router_id" ]] || ip netns list | awk '{print $1}' | grep -Fxq "qrouter-$router_id"; then
            router_ready=true
        fi
    fi
    if [[ "$api_ready" == true ]] &&
       [[ "$router_ready" == true ]] &&
       jq -ne --argjson agents "$agents" \
           'all(["L3 agent", "Open vSwitch agent", "DHCP agent"][]; . as $kind |
           any($agents[]; .["Agent Type"] == $kind and (.Alive == true or .Alive == ":-)")))' >/dev/null; then
        if jq -e 'any(.[]; .State == "up" and .Status == "enabled")' <<< "$compute" >/dev/null; then
            ready=true
            break
        fi
    elif [[ "$api_ready" == true && "$rpc_recovered" == false ]] && ((SECONDS >= agent_grace)); then
        # Processes may be active while their startup RPC requests never recover.
        # Restart the server before clients, once, then keep the original deadline.
        rpc_recovered=true
        echo 'Network agents or router namespace are not ready; recovering Neutron RPC and agents once.'
        for unit in devstack@neutron-rpc-server.service devstack@q-l3.service devstack@q-dhcp.service devstack@q-agt.service; do
            for installed in "${services[@]}"; do
                if [[ "$installed" == "$unit" ]]; then
                    remaining=$((deadline - SECONDS))
                    ((remaining > 0)) || break
                    ((remaining <= 60)) || remaining=60
                    timeout "$remaining" systemctl restart "$unit"
                    break
                fi
            done
        done
    fi
    sleep 2
done
if [[ "$ready" != true ]]; then
    systemctl --no-pager --failed
    echo 'OpenStack APIs, compute or network agents did not recover' >&2
    exit 1
fi

# A clean host shutdown can leave a tenant SHUTOFF. Also recover labs created
# before Nova's resume_guests_state_on_host_boot setting was introduced.
if jq -e 'any(.[]; .Name == "latch-vm")' <<< "$servers" >/dev/null; then
    instance=$(timeout 10 openstack server show latch-vm -f value -c OS-EXT-SRV-ATTR:instance_name)
    started=false
    tenant_ready=false
    for ((i=0; i<180; i++)); do
        state=$(timeout 10 openstack server show latch-vm -f value -c status)
        [[ "$state" != ERROR ]] || { echo 'Tenant is in ERROR state' >&2; exit 1; }
        if [[ "$state" == SHUTOFF && "$started" == false ]]; then
            timeout 30 openstack server start latch-vm
            started=true
        elif [[ "$state" == ACTIVE ]] && [[ $(timeout 10 virsh domstate "$instance" 2>/dev/null) == running ]]; then
            tenant_ready=true
            break
        fi
        sleep 2
    done
    [[ "$tenant_ready" == true ]] || { echo "Tenant did not recover: Nova=$state; libvirt domain must be running" >&2; exit 1; }
fi
echo 'OpenStack APIs, compute and network agents are ready.'
