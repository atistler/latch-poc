# Repository layout

`./lab` is the main entrypoint. The root `server-grant`, `server-connect`,
`server-status` and `server-revoke` commands delegate to its server commands. It dispatches to Python orchestration in `scripts/`,
which synchronizes the required sources to the Linux host and runs guest scripts
or Compose services. Generated images, keys, credentials and local runtime state
belong under ignored `.state/`, outside source control.

| Location | Responsibility and where changes belong |
| --- | --- |
| `scripts/lab.py` | CLI parsing, core lifecycle, tenant access and verification orchestration |
| `scripts/lima_backend.py` | ARM64 Linux host creation, acceleration selection, source synchronization and Compose invocation |
| `scripts/warpgate.py`, `scripts/certificates.py` | Gateway bootstrap/client SSH and laptop-side SSH certificate issuance |
| `scripts/server_integration.py` | Server API deployment, trusted inventory and laptop grant client |
| `scripts/export_openapi.py`, `openapi.yaml` | Reproducible combined API contract generated from the actual applications |
| `scripts/device_integration.py`, `scripts/network_integration.py` | Optional LDAP/device foundation and freeRouter/TACACS+ lifecycle |
| `guest/` | OpenStack host installation, Neutron/provider networking, tenant provisioning, boot recovery and live VM checks |
| `latch_api/` | Separate vendor-data, server-grant and device-grant applications; platform profiles, OpenStack adapters and durable access cleanup |
| `latch_common/warpgate.py` | Shared Warpgate admin API client used by CLI and both access services |
| `latch_common/cloud_init.py` | Shared tenant platform configuration renderer used by DynamicJSON profiles and the guest StaticJSON helper |
| `containers/` | [Service image/helper map](containers.md), including the pinned freeRouter source patch |
| `containers/openstack/` | Legacy Docker/QEMU OpenStack host image and entrypoint |
| `config/` | Checked-in DevStack, LDAP, TACACS+, device and inventory templates; runtime secrets are generated separately |
| `tests/` | Offline contracts/unit tests and explicit real-service harnesses |
| `docs/` | Task index, quick start, architecture, guides, reference, validation, proposed designs and archive |

All project documentation lives under `docs/`. Start at the
[documentation index](../README.md), use the [getting-started guide](../getting-started.md)
to build the lab, or read the [infrastructure overview](../architecture/infrastructure.md)
for network boundaries and access flows.

Keep orchestration in `scripts/`, host configuration in `guest/`, API behavior
in `latch_api/`, and behavior shared by installed services and host tools in
`latch_common/`. A new shared module must also be included in the relevant
container build and host synchronization paths. Update the matching tests and
operating guide when changing a lifecycle or access contract.

## Compose files

There are two alternative backend definitions. Use `./lab` for routine lifecycle
work; it selects the backend and enables the required optional profiles.

| File | Purpose |
| --- | --- |
| `compose.lima.yaml` | Default services inside Lima: Warpgate, SSH-CA OpenBao and vendor-data Latch API; OpenStack runs on the Linux host. The server API is enabled after provisioning through the `servers` profile; other optional services use the profiles below. |
| `compose.legacy.yaml` | Retained Docker/QEMU backend, including the OpenStack host container and core services; unused by the default backend |

Profiles in `compose.lima.yaml`:

| Profile | Services and CLI |
| --- | --- |
| Default (no profile) | Core Warpgate, OpenBao and Latch API, managed by `./lab up` |
| `servers` | Server grant API with isolated OpenStack/Warpgate credentials, enabled by `./lab provision` and resumed by `./lab up` |
| `device` | OpenLDAP, a separate LDAP-secrets OpenBao and device grant API; enabled by `./lab network-up` |
| `network` | freeRouter, tac_plus-ng and the restricted device-router; `./lab network-up` enables both `device` and `network` |
| `diagnostics` | Provider probe used by the VM isolation checks |

## Verification

See [validation procedures](../validation/README.md) for offline checks and real-service
harnesses, and [current validation status](../validation/status.md) for recorded coverage.

Server naming preserves the existing private `.state/vm/` credential directory
and `vm-grants` Docker volume so upgrading does not discard deployed secrets
or durable grants. These storage names are implementation details.
