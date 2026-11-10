#!/bin/bash
source /home/stack/latch/guest/common.sh
os_auth
echo 'Neutron agents (L3, OVS and DHCP must be alive):'
openstack network agent list
agents=$(openstack network agent list -f json)
for agent_type in 'L3 agent' 'Open vSwitch agent' 'DHCP agent'; do
    jq -e --arg kind "$agent_type" \
        'any(.[]; .["Agent Type"] == $kind and (.Alive == true or .Alive == ":-)"))' \
        <<< "$agents" >/dev/null || { echo "No live $agent_type" >&2; exit 1; }
done
ip netns list | grep qrouter-
if [[ -f "$DATA/acceleration.json" ]]; then
    cat "$DATA/acceleration.json"
    instance=$(openstack server show latch-vm -f value -c OS-EXT-SRV-ATTR:instance_name)
    expected=$(jq -r .virt_type "$DATA/acceleration.json")
    virsh dumpxml "$instance" > "$DATA/verified-domain.xml"
    python3 - "$DATA/verified-domain.xml" "$expected" <<'PY'
import sys
import xml.etree.ElementTree as ET
domain = ET.parse(sys.argv[1]).getroot()
assert domain.get('type') == sys.argv[2], 'Nova domain does not use the selected accelerator'
assert domain.find('os/type').get('arch') == 'aarch64', 'Tenant is not ARM64'
assert domain.find('os/loader') is not None, 'Tenant UEFI firmware is missing'
print('PASS: ARM64 tenant domain uses ' + sys.argv[2])
PY
fi
