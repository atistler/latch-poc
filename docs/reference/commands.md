# Command reference

Run `./lab --help` for the CLI syntax. Run commands from the repository root.
The default backend is Lima; server grants and the optional device integration
require it. The retained legacy backend keeps its earlier shared-path grant/revoke
behavior. Lima refuses the old guest grant/revoke scripts; use the API-backed CLI.

| Task | Command |
| --- | --- |
| Check host prerequisites | `./lab doctor` |
| Start or resume the Linux host and core services | `./lab up` |
| Install OpenStack on a new host | `./lab install` |
| Follow installation logs | `./lab logs` |
| Inspect lab status | `./lab status` |
| Provision tenant resources and deploy server grant coordination | `./lab provision` |
| Verify real server access and certificate policy | `./lab verify` |
| Open an Ubuntu tenant shell through Warpgate | `./server-connect --grant GRANT_UUID` |
| Open the OpenStack host shell | `./lab ssh` |
| List recorded gateway sessions | `./lab recordings` |
| Create or revoke the default/current server grant | `./lab grant` / `./lab revoke` |
| Stop services and the Linux host, retaining state | `./lab down` |

The [quick start](../../README.md#build-and-run) explains installation order and
resource requirements. The [infrastructure overview](../../INFRASTRUCTURE.md)
explains network isolation and the separate host and tenant shells.

## Server grants

Server grants require the Lima backend and a completed `./lab provision`.

| Task | Command |
| --- | --- |
| Create a grant for the default inventory server | `./server-grant --ttl 300` |
| Select an authorized server explicitly | `./server-grant --server SERVER_UUID --ttl 120` |
| Inspect the current grant | `./server-status` |
| Inspect a particular grant | `./server-status --grant GRANT_UUID` |
| Connect using an existing grant | `./server-connect --grant GRANT_UUID` |
| Revoke the current grant | `./server-revoke` |
| Revoke a particular grant | `./server-revoke --grant GRANT_UUID` |

`server-grant` prints and remembers the grant ID. The lifetime is 1–300 seconds,
with a 300-second default. `--server` can be omitted when inventory contains
one authorized server. Request a grant first, then pass its ID to
`server-connect --grant GRANT_UUID`; connecting never issues a grant.
Disconnecting leaves the grant active until revoke or expiry. Status and revoke
use the remembered grant when `--grant` is omitted. Revoking one grant leaves
another grant's shared network path intact.

The root commands delegate to `./lab server-grant`, `./lab server-connect`,
`./lab server-status` and `./lab server-revoke` respectively. The OpenStack
selection option `--instance` is retained as an alias for `--server`.
Server terminology covers physical hosts and VMs; the implemented inventory
adapter currently provisions OpenStack VMs. Physical-host inventory and network
adapters are not implemented yet.

Neutron API acceptance of a forwarding rule does not guarantee the data path
has converged immediately. The live harness probes gateway-to-server connectivity
before attempting SSH; temporary convergence delay is separate from grant
authorization.

A grant authorizes an `ubuntu` shell. Server readonly/operator/admin policies remain
proposed and are not CLI options. The [access workflow](../guides/access-workflows.md#server-access)
explains host-key checks, certificates and cleanup. HTTP contracts and the
ReDoc endpoint are in the [API reference](api.md).

## Network-device integration

| Task | Command |
| --- | --- |
| Build and start freeRouter/TACACS+ and their LDAP foundation | `./lab network-up` |
| Run real LDAP, SSH, command-policy and lifecycle acceptance | `./lab network-verify` |
| Request a read-only grant | `./lab device-grant --device freertr --role readonly --ttl 300` |
| Request configuration and save access | `./lab device-grant --device freertr --role operator --ttl 300` |
| Request unrestricted device administration | `./lab device-grant --device freertr --role admin --ttl 300` |
| Open an interactive device shell | `./lab device-connect --grant GRANT_UUID` |
| Revoke a device grant | `./lab device-revoke --grant GRANT_UUID` |
| Stop only the freeRouter and TACACS+ services | `./lab network-down` |

Use the grant ID returned by `device-grant`. The
[network-device guide](../guides/network-device-lab.md) defines the command
role permissions, credential handling, persistent state and validation limits.
`network-verify` temporarily stops and restores optional LDAP/TACACS+ services;
it disrupts device access while testing failure behavior.
