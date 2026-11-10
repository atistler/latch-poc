# Troubleshooting

Run commands from the repository root unless a command is explicitly inside the host or tenant shell.
For installation order, see [getting started](../getting-started.md).

Inside `./lab ssh`:

```sh
cat /opt/latch/acceleration.json
source /opt/stack/devstack/openrc admin admin
/opt/stack/data/venv/bin/openstack network agent list
/opt/stack/data/venv/bin/openstack server show latch-vm
/opt/stack/data/venv/bin/openstack console log show latch-vm
sudo virsh list --all
sudo ip netns list
sudo ovs-vsctl show
sudo journalctl -u devstack@q-l3 -n 100
```

From the laptop:

```sh
limactl list latch-arm64
limactl shell latch-arm64 -- sudo docker compose -f /home/stack/latch/compose.lima.yaml ps
limactl shell latch-arm64 -- sudo docker compose -f /home/stack/latch/compose.lima.yaml logs warpgate openbao latch-api
limactl shell latch-arm64 -- sudo docker compose -f /home/stack/latch/compose.lima.yaml exec warpgate ip route
```

Latch API can be inspected from the Linux host, without publishing it on the laptop:

```sh
limactl shell latch-arm64 -- curl -fsS http://127.0.0.1:8300/health/ready
limactl shell latch-arm64 -- sudo docker compose -f /home/stack/latch/compose.lima.yaml logs latch-api
```

Inside a newly created DynamicJSON tenant, inspect the config-drive response:

```sh
sudo mkdir -p /mnt/config-drive
sudo mount -o ro /dev/disk/by-label/config-2 /mnt/config-drive
sudo cat /mnt/config-drive/openstack/latest/vendor_data2.json
sudo cloud-init status --long
ssh-keygen -lf /etc/ssh/latch-user-ca.pub
sudo umount /mnt/config-drive
```

The API uses Nova's `cloud-init` target name and returns a JSON string, producing a single `cloud-init` key in `vendor_data2.json`. It ignores tenant metadata as configuration policy. Delivery failure is fatal to a new instance build; existing tenant SSH does not depend on the vendor-data application remaining available. The separate server API coordinates new grants and active-session cleanup. The initial POC trusts the Linux host and management network; add TLS and Nova-compatible service authentication before exposing this application more broadly.

For compatibility with this Nova version's config-drive builder, providers are `StaticJSON,DynamicJSON` with no static file path. The legacy `vendor_data.json` contains only `{}`; Latch supplies the CA and platform policy exclusively through `vendor_data2.json`. Compute and metadata services read separate configuration files (`nova-cpu.conf` and `nova.conf`), so both need the provider settings.

For Lima, use the explicit commands above to inspect the ARM64 services. The backend definitions are `compose.lima.yaml` and `compose.legacy.yaml`; avoid inspecting a different Docker daemon or backend by accident.

If installation fails, inspect `./lab logs` and `/opt/stack/logs/stack.sh.log`, correct the error and rerun `./lab install`. Systemd rejects a concurrent installation. Provisioning reuses named resources but is not transactional; partial failures can require repairing an incomplete resource inside the disposable cloud. If the negative connectivity check fails, inspect security groups and NAT behavior before accepting the access restriction. If Nova is ACTIVE but SSH times out, inspect the console and cloud-init logs.
