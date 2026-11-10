# Documentation

Start with the [repository quick start](../README.md) to build and run the POC,
or the [infrastructure overview](../INFRASTRUCTURE.md) for the deployed networks
and access flows. Run commands from the repository root unless stated otherwise.

## Architecture

- [Architecture and diagram sources](architecture/README.md): local deployment
  and logical network views.
- [Network-device architecture](guides/network-device-lab.md#logical-architecture):
  Warpgate, temporary LDAP accounts, freeRouter and TACACS+.

## Guides

- [Access workflows](guides/access-workflows.md): network reachability, gateway
  admission, target authentication/authorization, recording and revocation for
  both network devices and OpenStack VMs.

- [Open source network-device lab](guides/network-device-lab.md): build, connect,
  role policies, secrets, recovery and current acceptance status.
- [Device access and credential lifecycle](guides/device-access.md):
  LDAP/device API foundation, grant policy and cleanup.
- [Vendor-data cutover and recovery](guides/vendor-data-cutover.md): replace a
  tenant deliberately while preserving its original VM and disk.

## Reference

- [API reference](reference/api.md): ReDoc endpoints, OpenAPI contract, access
  restrictions and schema regeneration.
- [Command reference](reference/commands.md): lifecycle, server access and optional
  network-device commands.
- [Repository layout](reference/repository-layout.md): modules, Compose profiles,
  change ownership and test types.
- [Network-device acceptance checklist](reference/network-open-source-plan.md):
  required live evidence and a link to current validation status.

## Archive

[Historical designs and validation](archive/README.md) retain the ARM64 migration,
Boundary-to-Warpgate transition and vendor-data work.
They describe the system at the time of that work; use the current guides for
operating the lab.
