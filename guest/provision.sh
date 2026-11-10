#!/bin/bash
source /home/stack/latch/guest/common.sh
[[ -f "$DATA/install-complete" ]] || { echo 'Run ./lab install first'; exit 1; }
os_auth
bash "$BASE/guest/time-configure.sh"
bash "$BASE/guest/vendor-data-configure.sh"
openstack extension list --network -f value -c Alias | grep -qx floating-ip-port-forwarding
if ! openstack network show latch-provider >/dev/null 2>&1; then
    openstack network create latch-provider --external --provider-network-type flat \
        --provider-physical-network public
    openstack subnet create latch-provider-subnet --network latch-provider \
        --subnet-range 100.64.10.0/24 --gateway 100.64.10.1 --no-dhcp \
        --allocation-pool start=100.64.10.100,end=100.64.10.249
fi
if ! openstack network show latch-tenant >/dev/null 2>&1; then
    openstack network create latch-tenant
    openstack subnet create latch-tenant-subnet --network latch-tenant \
        --subnet-range 192.168.20.0/24 --gateway 192.168.20.1
fi
if ! openstack router show latch-router >/dev/null 2>&1; then
    openstack router create latch-router --centralized --no-ha
    openstack router set latch-router --external-gateway latch-provider
    openstack router add subnet latch-router latch-tenant-subnet
fi
if ! openstack security group show latch-ssh >/dev/null 2>&1; then
    openstack security group create latch-ssh
    openstack security group rule create latch-ssh --ingress --ethertype IPv4 \
        --protocol tcp --dst-port 22 --remote-ip "$WORKER/32"
fi
if ! openstack flavor show latch-ubuntu >/dev/null 2>&1; then
    openstack flavor create latch-ubuntu --ram 1024 --disk 8 --vcpus 1
fi
if ! openstack port show latch-vm-port >/dev/null 2>&1; then
    openstack port create latch-vm-port --network latch-tenant \
        --fixed-ip subnet=latch-tenant-subnet,ip-address="$TENANT" --security-group latch-ssh
fi
if ! openstack server show latch-vm >/dev/null 2>&1; then
    if ! openstack image show latch-ubuntu-noble >/dev/null 2>&1; then
        image_dir="$DATA/ubuntu-image"
        mkdir -p "$image_dir"
        url=https://cloud-images.ubuntu.com/noble/current
        arch=amd64
        image_properties=()
        if [[ "$(uname -m)" == aarch64 ]]; then
            arch=arm64
            image_properties=(--property architecture=aarch64 --property hw_machine_type=virt --property hw_firmware_type=uefi)
        fi
        filename="noble-server-cloudimg-$arch.img"
        curl -fL --retry 3 "$url/SHA256SUMS" -o "$image_dir/SHA256SUMS"
        curl -fL --retry 3 "$url/$filename" -o "$image_dir/$filename"
        (cd "$image_dir" || exit; awk -v file="$filename" '$2 == file || $2 == "*" file {print}' SHA256SUMS > selected.sha256
            [[ -s selected.sha256 ]]; sha256sum -c selected.sha256)
        openstack image create latch-ubuntu-noble --disk-format qcow2 --container-format bare \
            "${image_properties[@]}" --file "$image_dir/$filename"
    fi
    openstack server create latch-vm --image latch-ubuntu-noble --flavor latch-ubuntu \
        --port latch-vm-port --config-drive true \
        --property latch_auth=openbao-v1 --property latch_bootstrap=dynamic-vendor-data-v1
else
    if ! openstack server show latch-vm -f json | jq -e '.properties.latch_auth == "openbao-v1"' >/dev/null; then
        echo 'Existing latch-vm predates certificate authentication. Rename or delete that disposable VM before provisioning.' >&2
        exit 1
    fi
    if ! openstack server show latch-vm -f json | jq -e '.properties.latch_bootstrap == "dynamic-vendor-data-v1"' >/dev/null; then
        echo 'Existing latch-vm retains its original config drive. New vendor data applies to newly created VMs only.' >&2
    fi
fi
for ((i=0; i<360; i++)); do
    status=$(openstack server show latch-vm -f value -c status)
    [[ "$status" != ERROR ]] || { openstack server show latch-vm; exit 1; }
    [[ "$status" != ACTIVE ]] || break
    sleep 5
done
[[ "$status" == ACTIVE ]] || { echo 'VM did not reach ACTIVE in 30 minutes'; exit 1; }
fip_id=$(openstack floating ip list --floating-ip-address "$FIP" -f value -c ID)
if [[ -z "$fip_id" ]]; then
    openstack floating ip create latch-provider --floating-ip-address "$FIP"
fi
echo 'OpenStack resources provisioned. VM access is granted through the Latch API.'
