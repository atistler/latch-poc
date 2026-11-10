> Historical implementation coordination record. Use the [current acceptance checklist](../../validation/network-device-checklist.md) and [operating guide](../../guides/network-device-lab.md).

# Open source network-device POC

Implement the approved Warpgate → freeRouter SSH → tac_plus-ng → OpenLDAP flow.
OpenBao leases unique LDAP accounts; Latch manages grants and session cleanup.
Preserve the tenant VM, SSH CA, existing inventory.

## Tasks and interfaces

1. freeRouter container: native ARM64 JVM, pinned source, persistent SSH identity,
   remote command authorization, fail closed. Owner: freertr_build.
2. tac_plus-ng container: pinned source, LDAPS user bind and role groups,
   default-deny command policy. Owner: tacacs_build.
3. Optional Compose overlay and lifecycle: network-up/verify/down, trusted inventory
   registration and real SSH/authorization/revocation/recording tests. Owner: network_cli.
4. CLI wiring, documentation, diagrams, final review and runtime validation. Owner: root.

Shared contracts: service names `freertr` and `tacacs`; management bridge
`10.1.3.0/24`; shared secret file `/run/network-secrets/tacacs-secret`;
existing LDAP reader and CA from the LDAP foundation; no host ports.

## Progress

- Started implementation using separate owned files for three parallel tasks.
- CLI wiring added; Lima source synchronization includes the optional overlay.
- Existing six supporting containers confirmed healthy before deployment.
- Repository contains untracked project files and no established commit baseline;
  review will inspect the named new/modified files directly and preserve user work.
- Live acceptance remains pending until the complete network path is tested.

## Interface review

| Tasks | Contract | Review |
| --- | --- | --- |
| 1–2 | TACACS host, shared secret, command tokens | Agents coordinating policy details |
| 1–3 | SSH port, persistent host key export | Agent coordination required before deployment |
| 2–3 | LDAPS CA and reader secret mounts | Existing foundation supplies both |
| 3–4 | up(root,state,options), verify(root,state,options), down(root,state,options) | Agreed |

Each task must pass implementation review and live integration before completion.
