# Latch-coordinated server access

> Historical implemented design. The renamed server API/CLI passed full live
> acceptance at `2026-11-04T15:58:51Z`; this records that implementation stage.
> Use the [current documentation](../../README.md) and
> [validation status](../../validation/status.md) for deployed behavior and
> current coverage. [Server roles](../../design/server-roles.md) remain proposed.

## Intent

Latch becomes the entry point for granting, inspecting and revoking server access.
The client requests access and uses the returned SSH connection details. It no
longer configures Neutron, Warpgate or OpenBao itself. Keep the existing
agentless SSH data path and session recording.

User requirements: coordinate network access, Warpgate connectivity and SSH
certificate injection through the Latch API; provide a checked-in `openapi.yaml`
and ReDoc at `/docs`. Continue excluding ServiceNow and Entra ID integration.

## Behavior before implementation

Provisioning created a Neutron forwarding rule. The CLI configured a shared
`latch` Warpgate user and `latch-vm` target. Warpgate authenticates to OpenBao
using AppRole and requests an SSH certificate for its outbound key. The VM
trusts the OpenBao CA delivered through dynamic vendor data. The CLI reads the
VM host key from Nova console output and pins it. Server access had no individual
grant lifecycle. Device access already had durable grants, reconciliation
and an authenticated API; its patterns and tested gateway client informed this implementation.

## Implemented API contract

Authenticated access operations live in the privileged access application;
the Nova vendor-data application stays separate and has no signing or Neutron
credentials. Both are parts of Latch, with documented deployment URLs.
Retain existing device routes. Server access is available without enabling the
optional network-device stack or LDAP OpenBao.

- `POST /v1/server-grants`: instance UUID, laptop SSH public key, TTL (default and
  maximum 300 seconds), and an optional `Idempotency-Key` header. Returns 201 for an active grant.
- `GET /v1/server-grants/{grant_id}`: lifecycle state, expiry and connection details.
- `DELETE /v1/server-grants/{grant_id}`: removes admission and closes that grant's
  sessions, then releases its network lease. Repeated deletion is safe. Return
  a cleanup-pending state if dependencies prevent complete cleanup.

The API token maps to a server-controlled caller identity. Authorization uses
trusted inventory containing instance UUID, OpenStack project, permitted caller,
VM login account and expected host-key fingerprint. Requesters cannot supply
an IP, port, signing role, username or arbitrary principal. Another caller's
grant returns 404. Idempotency is scoped to caller and payload; conflicting
reuse returns 409. POC authentication follows the existing bearer-token model.

Responses contain grant ID, instance ID, state, expiry, gateway SSH host/port,
Warpgate username and target selector, and the pinned gateway host key. No
password, target private key, signer token or AppRole secret is returned. Client
builds an SSH argument list, avoiding shell interpolation and path quoting bugs.

## Grant workflow

1. Authenticate caller, validate public key and authorize the inventory entry.
2. Persist an issuing intent and expiry before making external changes.
3. Resolve the instance and its Neutron port using project-scoped OpenStack
   APIs. Confirm actual project, instance and port match inventory.
4. Validate the VM host key against trusted inventory. Enrollment can obtain a
   candidate from Nova console output over the authenticated control plane;
   missing, ambiguous or changed keys fail closed. Ordinary grants cannot
   silently replace pins. OpenStack login keypairs are not VM host keys.
5. Acquire a durable network lease. Only the gateway provider address
   `100.64.10.10/32` can reach SSH through the floating IP forwarding rule.
6. Create a unique Warpgate user, role and SSH target for this grant. Attach
   the laptop public key to that user. Configure the role's expiry, restrict
   its target assignment, pin the VM key and require recording.
7. Configure certificate authentication against the existing core OpenBao
   backend. Warpgate requests a short-lived certificate at connection time
   and presents it to the VM. Latch coordinates injection; OpenBao signs;
   Warpgate retains the outbound private key. Do not return a certificate
   intended for direct laptop-to-VM authentication.
8. Mark active only when all steps succeed; otherwise start compensating
   cleanup and do not return usable admission details.

The baseline preserves the current `ubuntu` certificate principal and VM
permissions. Enforcing the [proposed roles](../../design/server-roles.md) is a separate stage. Constrain
the signing role and backend path to that login identity. Use bounded
certificate TTLs; grant admission and session shutdown enforce the grant
deadline because certificate expiry alone does not terminate an SSH session.

## Networking and revocation

Builder choice: share one Latch-owned forwarding rule per instance and hold
durable per-grant leases. Keep the current `100.64.10.120:22001` endpoint for
the existing VM. Serialize network acquire/release through the durable store;
delete forwarding only when no live or issuing grant needs it. A unique
gateway user and target provide independent admission despite a shared path.
Persist grant intent and a trusted inventory snapshot before external changes.
Discover partially-created gateway resources by deterministic grant names and
Neutron resources by ownership markers during recovery.

Manage a Latch-owned SSH security-group rule rather than broadening arbitrary
tenant rules. Never delete unrelated rules, forwarding entries or sessions.
Revoke target-role admission first and close sessions belonging to the grant,
then remove its user and role and release its network lease. Warpgate deletes
an endpoint host-key pin when deleting a target, so retain disabled targets
while another grant shares that endpoint. Final cleanup collects disabled
targets and releases the shared pin and owned network access. Retry failed cleanup from a
two-second reconciler, including on startup. Expiry uses the same workflow.
Retain audit state after cleanup. A dependency outage may delay session closure
or network deletion; status must report that honestly rather than claim revoked.
Warpgate role expiry independently limits new admissions during API downtime.

The API uses a project-scoped OpenStack service credential and direct Nova/
Neutron APIs. It runs on service management, with no Docker socket, provider
interface or tenant interface. Provisioning creates base VM/network resources
but no longer opens access automatically. Migrate the old shared gateway user
and forwarding only when no legacy session is active, or via an explicit
operator cutover; remove the legacy grant bypass when the API path is active.

## Packaging, documentation and CLI

Use the current Python/FastAPI stack. Separate lifecycle logic, OpenStack and
gateway adapters, API models and configuration. Reuse device abstractions
where they fit without coupling server grants to LDAP. Mount credentials privately,
use unprivileged request handling, and persist grant state in a volume.

Serve ReDoc at `/docs` and the machine-readable schema at `/openapi.json` on
each application. Produce a root `openapi.yaml` describing public Latch
operations, with operation-specific server URLs for vendor-data and access
applications. Give operations explicit response models, examples, bearer
security and documented error responses. The vendor-data response remains a
JSON string, as Nova expects. Generate the checked-in schema from application
schemas and check for drift; do not document planned VM-role behavior as live.
Serve the YAML at `/openapi.yaml` for convenient download. Decide whether to
bundle ReDoc assets during implementation; document if browser internet access
is needed for a CDN asset.

The server grant/status/revoke CLI commands use the API. `./server-grant`
issues a grant before `./server-connect --grant GRANT_UUID` connects. Connecting
requires an explicit grant, never issues one and does not revoke on disconnect.
The grant remains active until expiry or explicit revoke. `server-grant`
remembers the default grant for status/revoke. Equivalent `./lab server-*`
commands use the same implementation. `--server SERVER_UUID` selects inventory;
`--instance` remains an OpenStack compatibility alias. The current inventory and
network adapter supports OpenStack VMs; physical-host support remains future
work behind the server access naming.

## Acceptance evidence

The server naming/CLI update passed 197 offline tests (196 passed, one skipped).
The exact `./server-grant` → `./server-connect --grant GRANT_UUID` flow passed
live SSH; disconnect preserved the grant and `./server-revoke` revoked it.
The full renamed live acceptance suite also passed at `2026-11-04T15:58:51Z`:
API authentication and authorization, two grants sharing one forwarding, API
restart preserving sessions, independent revoke and surviving-grant reconnect,
recording playback, final network removal, normal expiry and cleanup after
expiry during API downtime. Evidence: `.state/lima/server-verification.json`.
The prior full lifecycle results below remain historical baseline evidence.

The baseline live run passed recorded certificate SSH, concurrent grants,
independent revoke, final network removal, active expiry, API restart and expiry
reconciliation after API downtime. It also rejected missing/wrong bearer tokens,
unauthorized inventory and destination/TTL overrides. Local evidence:
`.state/lima/vm-verification.json`. Final `./lab verify` passed network isolation,
certificate-policy rejection and recording playback. The offline suite ran 194
tests with 193 passes and one integration-only skip, including wrong-pin and
partial-cleanup cases. These results do not implement VM role restrictions.

The following acceptance criteria distinguish real SSH coverage from additional
offline failure-path coverage:

Exercise real SSH through Warpgate with recording and CA authentication; deny
unsigned/direct access, unauthorized instances, wrong pins and another caller's
grant. Two simultaneous grants to one VM must survive independent revocation.
Prove explicit revocation and timed expiry close the affected session and deny
reconnection, and the last grant closes the forwarding path. Verify API restart
reconciliation, partial provisioning failure cleanup and dependency outages.
Verify CLI paths containing spaces. Parse the generated YAML, check operation
coverage/security and schema drift, and load `/docs`, `/openapi.json` and
`/openapi.yaml`. Run existing vendor-data and device regression checks.

## Proposed follow-up

The unimplemented [server-role design](../../design/server-roles.md) is kept
separately from this implemented baseline. Current server grants still use the
existing `ubuntu` login; they do not enforce readonly/operator/admin roles.
