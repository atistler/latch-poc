# Server access roles

Status: **proposed; not implemented**. Current server grants use the existing
`ubuntu` account and do not enforce these role restrictions. This proposal was
extracted from the [implemented server-access design](../archive/designs/2026-11-03-server-access.md).
It is separate from the implemented network-device role policy.

## Proposed behavior


User selected a **strict diagnostics interface** for readonly, rather than a
normal Linux shell without sudo. A normal unprivileged shell is not read-only:
it can write user files and run arbitrary programs.

- `readonly`: dedicated local account, no sudo, root-owned forced-command
  dispatcher with a small command grammar. Initial ideas: system status,
  bounded log reads, service status, IP/route inspection, bounded ping and
  connectivity probes. No arbitrary shell, SFTP/SCP, forwarding, editors,
  command substitution, output redirection or user-selected executables.
  Approve readable paths and log sources; diagnostics may expose secrets.
- `operator`: separate local account; approved root-owned wrappers for actions
  such as restarting selected application services and deploying validated
  application configuration. No generic `sudo systemctl`, editors, package
  manager, Docker membership, arbitrary file writes or changes to SSH, sudo,
  identity, audit and network policy. Service unit files and executed binaries
  must be protected from operator writes to prevent indirect root execution.
- `admin`: separate local account with full sudo, recorded SSH and short-lived
  admission. Once root is granted, the administrator can change local policy;
  recording is an audit mechanism, not a guarantee against root tampering.

Represent roles using distinct certificate principals and Linux accounts,
separate signing policies and trusted server-side role mappings. Never place
all users into the current privileged `ubuntu` account and assume a certificate
label restricts their commands. Bootstrap root-owned accounts and policy
through vendor data; do not add a guest agent. Existing VMs need an explicit
policy installation or rebuild because config-drive changes are not retroactive.

Proposed role request extension: an explicit `role` field authorized against
caller + instance + role; no implicit promotion to admin. Grant audit associates
caller, role, certificate identity where available, target and recording.
Test cross-role login rejection, shell/transfer escape attempts, privilege
escalation through operator wrappers, and role-specific allow/deny behavior.
The exact readonly diagnostics and operator change capabilities still require
agreement; this document does not claim these roles are implemented.
