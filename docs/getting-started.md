# Getting started

Build the local OpenStack lab, request a temporary server grant, and connect
through Warpgate. OpenBao supplies the SSH user certificate; no target agent is
required. Run every command below from the repository root.

The default backend is an ARM64 Linux VM managed by Lima. OpenStack runs directly
in that VM; Warpgate, OpenBao and the Latch APIs run in separate containers.
See the [infrastructure overview](architecture/infrastructure.md) for deployment
and networking details, and [validation status](validation/status.md) for known
limits. Server roles and physical-server adapters are not implemented.

## Requirements

- Apple Silicon Mac, macOS 13.5 or later; nested acceleration requires supported M3-or-later hardware and macOS 15+.
- Lima (`brew install lima`), Python 3.10+, OpenSSH and `tar`. Docker Desktop is not required for the Lima backend.
- Default Linux VM allocation: 8 GiB RAM, six CPUs and an 80 GiB sparse disk. Leave memory for macOS; a 16 GiB or larger Mac is preferable for this complete cloud.
- Internet access to Ubuntu cloud images/APT, Docker Hub, GHCR, OpenDev, PyPI and Warpgate releases.

The stable OpenStack branch, current Ubuntu cloud images and packages are moving upstream inputs. This is a disposable connectivity lab, not a production cloud or a performance benchmark.

## Build and run

```sh
brew install lima
cp .env.example .env
./lab doctor
./lab up
./lab install
./lab logs
```

Configure resources and ports in `.env` before creating the Linux VM. Its resource
and acceleration settings are creation-time choices; changing them for an existing
VM is rejected. `LAB_ACCEL=auto` probes nested KVM, `tcg` forces software emulation,
and `kvm` requires hardware acceleration. M1/M2 tenants use QEMU software emulation.

`up` starts Lima and the core containers. `install` launches the background
OpenStack installation. Wait for `Installation complete` in the log before
continuing. Ctrl-C stops log following without stopping installation.

```sh
./lab status
./lab provision
./lab verify
```

`provision` creates the tenant resources and deploys the server grant API. It waits
up to 30 minutes for Nova ACTIVE; ACTIVE precedes SSH readiness. Existing named
resources are reused. `verify` checks the real SSH path, isolation and certificate
policy; healthy containers alone do not establish acceptance.

## Connect to a server

```sh
./server-grant --ttl 300
./server-connect --grant GRANT_UUID
```

Replace `GRANT_UUID` with the ID returned by `server-grant`. The current server
login is `ubuntu`. Connecting uses an existing grant and never creates one.
Type `exit` to disconnect; the grant remains active until expiry or revocation:

```sh
./server-revoke --grant GRANT_UUID
```

Use `./lab ssh` for the OpenStack host's `stack` shell, which has sudo access.
That is separate from the tenant shell. See [server access](guides/server-access.md)
for target selection, trust, recordings and cleanup.

## Optional network-device access

After provisioning the core lab, follow the [network-device setup guide](guides/network-device-lab.md)
to start freeRouter, TACACS+ and the LDAP foundation. Request a device grant with
`./lab device-grant`, then use `./lab device-connect --grant GRANT_UUID`.
Device roles are implemented; their permissions differ from the proposed server roles.

## Stop and resume

```sh
./lab down
./lab up
./lab provision
./lab verify
```

`down` retains volumes, VM disks and laptop state. Deleting Lima or its volumes
can destroy the cloud and SSH CA. Read [operations and recovery](guides/operations.md)
before changing backends, resetting state or restoring backups.

## Next steps

- [Inspect failures and config-drive files](guides/troubleshooting.md).
- [View recordings and development credentials](guides/server-access.md#ssh-certificates-and-target-trust).
- [Understand authorization and grant lifecycle](guides/access-workflows.md).
- [Run offline and live validation](validation/README.md).
- [Browse all documentation](README.md).
