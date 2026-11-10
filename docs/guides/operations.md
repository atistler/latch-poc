# Lab operations and recovery

Run commands from the repository root. For diagnosis, see [troubleshooting](troubleshooting.md).

## Stop and resume

```sh
./lab down
./lab up
./lab provision
./lab verify
```

`down` removes supporting containers without deleting their volumes, then stops the Lima VM cleanly. OpenStack disks, service volumes and CA remain inside Lima; laptop keys remain in `.state/lima/`.

After an installed host starts, `up` restores the provider uplink and runs `guest/resume.sh`. It starts the installed OpenStack services, waits for API responses, live Nova/Neutron agents and the existing router’s namespace, and starts an existing `latch-vm` if it is SHUTOFF. It checks that the tenant’s libvirt domain is running before reporting readiness. If APIs respond but network agents remain stale or the router namespace is missing after 90 seconds, it restarts Neutron RPC followed by the installed L3, DHCP and OVS agents once; readiness still has a ten-minute deadline. Boot configuration orders services after the management address and their database/broker/OVS dependencies, retries failed DevStack processes, and enables Nova guest resume. An ERROR tenant causes recovery to fail for investigation. SSH readiness is established by `verify`. When trusted VM inventory is present,
`up` also restores the server API after OpenStack resumes, allowing its persisted
grant expiry and cleanup work to continue.

Warpgate database, host keys and recordings persist in its own volume. The retired Boundary volumes are retained for rollback; do not delete them or run Boundary concurrently on provider address `100.64.10.10`. Re-run `provision` and `verify` after restart to check configuration and access.

Short-lived certificates require synchronized clocks. The OpenStack host runs Chrony on `100.64.10.1:123/UDP`, and tenant cloud-init points systemd-timesyncd to that local source with a 16–32 second poll interval. The tenant needs no Internet NTP access. This corrects nested guest clock drift after laptop sleep; without it, SSH can reject a freshly issued certificate as “not yet valid.” The host follows upstream NTP and can temporarily serve its own clock at stratum 10 while offline. Installation and provisioning configure the host service; existing tenants created before this fix need the same timesyncd configuration applied inside the VM because cloud-init does not rerun when reprovisioning an existing server.

To return to the retained x86 environment after stopping Lima:

```sh
LAB_BACKEND=legacy ./lab up
LAB_BACKEND=legacy ./lab status
```

The legacy backend needs Docker Desktop/Engine with Compose v2 and at least 8 GiB allocated to Docker. It adds an x86 QEMU runner with its own Linux guest. Its defaults are four CPUs, 6 GiB guest memory and a 64 GB disk. Its `.state/` keys and Docker Desktop volumes are separate from Lima. Latch API/DynamicJSON provisioning and API-managed server grants are supported only on the Lima backend; the retained legacy backend has no Latch service or validated route to it. Stop one backend before starting the other on the same laptop ports.

Deleting a Lima VM destroys the OpenStack disk and its Docker volumes, including the CA. Back up the VM disk and `.state/lima/` together before any deliberate reset. A new CA requires reprovisioning target trust. Never commit `.state/` or `.env`.

## Backend boundaries

`LAB_BACKEND=legacy` selects Docker Desktop/QEMU instead of Lima. Its runner boots an x86 guest, adding a runner bridge/TAP/provider NIC to the path. Provider `.2` is that runner address; on Lima `.2` is only the temporary diagnostic probe. Legacy host management uses QEMU SLIRP `172.20.2.15` with gateway `.2` and DNS `.3`. Its state and service volumes are separate. DynamicJSON is supported only in Lima; legacy does not deploy Latch API. Stop one backend before starting the other on the same laptop ports.

The Lima backend uses `compose.lima.yaml`, laptop state under `.state/lima/`, and service volumes inside the Linux VM. Existing x86 disks and CA data are retained; they are not automatically imported into ARM64.
