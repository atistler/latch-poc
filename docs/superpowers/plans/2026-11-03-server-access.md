# Server Access Implementation Plan

> **For agentic workers:** Use subagent-driven implementation for independent modules and review their integration before acceptance.

**Goal:** Latch coordinates expiring server SSH access through the current OpenStack backend, with generated OpenAPI and ReDoc.

**Architecture:** Durable server grants own gateway identities and shared Neutron reachability leases. Warpgate retains certificate injection through core OpenBao. The privileged server API stays isolated from Nova vendor data and the optional LDAP stack.

**Tech Stack:** Python, FastAPI, SQLite, OpenStack REST APIs, Warpgate, OpenBao, Docker Compose and Lima.

**Spec:** ../specs/2026-11-03-server-access-design.md (baseline authorized; server roles remain proposals).

## Global Constraints

- TTL defaults to and cannot exceed 300 seconds.
- No target private keys or signing credentials in client responses.
- No agent, Docker socket or provider interface in the server API.
- Scope cleanup to Latch-owned resources; preserve concurrent grants and unrelated resources.
- Preserve vendor-data JSON-string response and device API behavior.

## Review Focus

- Lost upstream creation responses must permit deterministic cleanup.
- Concurrent grants must not remove each other's forwarding rule.
- Restart must reconcile expired and interrupted grants.
- Caller-supplied instance IDs must not bypass project or inventory authorization.
- Documentation must describe live operations and preserve bearer requirements.

### Task 1: API documentation

Files: shared API documentation helpers, existing app factories, schema generator, root openapi.yaml, API reference and contract tests.

- [x] Enable ReDoc /docs and schema downloads without weakening authentication.
- [x] Generate combined schema with distinct service URLs and documented responses.
- [x] Test routes, bearer requirements, schema drift and credential absence.

### Task 2: server grant domain and adapters

Files: latch_api/server_access.py, latch_api/openstack.py, latch_api/server_app.py, focused tests.

Interfaces: ServerAccess.issue(caller, server_id, public_key, ttl_seconds=300, idempotency_key=None), get(caller, grant_id), revoke(caller, grant_id), sweep(). OpenStack adapter resolves inventory and ensures/removes owned reachability. Gateway adapter provisions certificate-backed SSH targets and cleans deterministic grant names.

- [x] Test ownership, TTL, idempotency, independent grants, cleanup failures and restart recovery.
- [x] Implement durable shared network leases and isolated gateway identities.
- [x] Implement project-scoped OpenStack REST integration and pinned gateway setup.
- [x] Expose authenticated HTTP operations with startup/periodic reconciliation.

### Task 3: Runtime and CLI

Files: scripts/server_integration.py, scripts/lab.py, guest/provision.sh, Compose, server API Dockerfile, OpenStack credential bootstrap.

- [x] Prepare private service credential, inventory, trusted VM pin and bearer token.
- [x] Deploy independent server API service, add grant/status/revoke and connect via API.
- [x] Remove automatic provisioning grant and close legacy bypass at safe cutover.
- [x] Test commands with spaces in workspace paths and preserve unrelated optional services.

### Task 4: Integration acceptance

Files: live server API acceptance script, workflow and command documentation.

- [x] Run unit tests and generated schema drift check (194 run, 193 passed, one integration-only skip).
- [x] Deploy and demonstrate certificate-backed recorded SSH, including final `./lab verify` playback.
- [x] Validate two grants, explicit revocation, active expiry, unauthorized caller and API restart cleanup live; cover wrong-pin rejection and cleanup failures in offline tests.
- [x] Update documentation and diagrams with measured results and limitations.

Live evidence: `.state/lima/vm-verification.json`, passed at
`2026-11-04T03:19:22Z`. No Server readonly/operator/admin enforcement is included.

## Server naming and explicit-grant follow-up

Validated on 2026-11-04: 197 offline tests ran, with 196 passes and one
integration-only skip. The root grant/connect/revoke commands passed actual
SSH with disconnect preserving the grant. The renamed full lifecycle suite
passed at `2026-11-04T15:58:51Z`; current evidence is in
`.state/lima/server-verification.json`.
