# Latch

Latch is a proof of concept for temporary, controlled SSH access to servers and
network devices. It aims to give engineers the access they need for a specific
task, for a limited time, with recorded sessions and automatic cleanup.

The goal is to coordinate the whole access lifecycle: authorize a request, make
the target reachable, provide temporary credentials, admit the user through an
SSH gateway, and remove access when the grant expires or is revoked. Users
connect through Warpgate without installing an agent on the target. The Latch
APIs coordinate access; Warpgate carries and records the SSH sessions.

## How access works

An engineer requests an access grant for an authorized target and duration, then
connects using the returned grant ID. Latch prepares the gateway and credentials
for that grant. The two implemented access paths use different target controls:

- **Servers:** Latch validates an OpenStack VM against trusted inventory and
  coordinates restricted Neutron port forwarding. Warpgate obtains a short-lived
  SSH user certificate from OpenBao and connects to the VM. The certificate
  authority's public key is supplied during provisioning through Latch's dynamic
  vendor-data API.
- **Network devices:** Latch obtains a temporary LDAP account from OpenBao and
  configures Warpgate to use it. The device delegates login and command
  authorization to TACACS+, backed by OpenLDAP. The lab uses freeRouter with
  readonly, operator and admin policies.

Warpgate verifies target SSH host keys and records terminal sessions. Revocation
and expiry cleanup close managed sessions and remove temporary access resources.
Active-session termination depends on the cleanup services being available;
credential expiry alone does not close an established SSH session.

## What this repository proves

This repository builds a local lab for testing these workflows end to end. Its
default environment runs OpenStack in an ARM64 Linux VM managed by Lima on an
Apple Silicon Mac, with separate containers for the gateway, APIs and supporting
services. The optional network-device lab adds routed device management,
freeRouter, TACACS+ and the LDAP credential services.

The broader server access model includes physical servers, but the implemented
adapter currently supports OpenStack VMs. Server grants provide the existing
`ubuntu` account; server readonly, operator and admin roles remain proposed.
External identity-provider and change-management integrations are outside the
current POC. See [current validation status](validation/status.md) for tested
coverage and outstanding acceptance checks.

## Explore the project

Start with [getting started](getting-started.md) to build the lab, or the
[infrastructure overview](architecture/infrastructure.md) to understand the
deployment. Run commands from the repository root unless a guide says otherwise.

## Start with a task

| Task | Guide |
| --- | --- |
| Build the local lab | [Getting started](getting-started.md) |
| Request a grant and connect to a server | [Server access](guides/server-access.md) |
| Set up or connect to a network device | [Network-device lab](guides/network-device-lab.md) |
| Understand how access is granted and revoked | [Access workflows](guides/access-workflows.md) |
| Understand device credentials and lease cleanup | [Device credential lifecycle](guides/device-access.md) |
| Inspect failures or config-drive files | [Troubleshooting](guides/troubleshooting.md) |
| Stop, resume or recover the lab | [Operations and recovery](guides/operations.md) |
| Replace a tenant for vendor-data changes | [Vendor-data cutover](guides/vendor-data-cutover.md) |
| Check what has actually passed | [Current validation status](validation/status.md) |
| Run tests and acceptance checks | [Validation procedures](validation/README.md) |

## Understand the architecture

- [Infrastructure overview](architecture/infrastructure.md): deployment, networks, services and trust paths.
- [Architecture diagrams](architecture/README.md): deployment, logical networks and device access; includes diagram sources and regeneration.

## Reference

- [CLI commands](reference/commands.md).
- [API contract and ReDoc](reference/api.md).
- [Configuration sources](reference/configuration.md).
- [Container images and helpers](reference/containers.md).
- [Repository layout and module responsibilities](reference/repository-layout.md).
- [Shared Python components](reference/shared-components.md).
- [Network-device acceptance checklist](validation/network-device-checklist.md).

## Proposed designs and history

[Server roles](design/server-roles.md) describes the proposed strict diagnostics,
operator and admin interfaces. These roles are not implemented for servers.

[Historical designs and migration records](archive/README.md) preserve completed
planning work. [Validation reports](validation/README.md#recorded-evidence) preserve
test history. Use current guides for operating the lab.
