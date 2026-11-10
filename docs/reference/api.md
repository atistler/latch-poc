# Latch API reference

The checked-in [OpenAPI contract](../../openapi.yaml) describes the implemented
vendor-data and access operations. Operation-level `servers` identify the
application hosting each operation; the lab keeps these on management listeners.
The applications also serve their own schema and ReDoc documentation:

| Application | ReDoc | JSON schema | YAML schema |
|---|---|---|---|
| Vendor data | `http://127.0.0.1:8300/docs` on the Linux host | `/openapi.json` | `/openapi.yaml` |
| Optional device access | `http://device-api:8310/docs` on service management | `/openapi.json` | `/openapi.yaml` |
| Server access | `http://127.0.0.1:8320/docs` on the Linux host, or `http://server-api:8320/docs` on service management | `/openapi.json` | `/openapi.yaml` |

The server API is enabled by provisioning through the Compose `servers` profile. The
vendor-data API remains a separate application with no access-grant credentials.
Use the [server CLI commands](commands.md#server-grants) for routine access.

Documentation and schemas do not need an API token. Device and server grant operations
require their service's bearer token and enforce caller ownership and trusted
inventory policy. Device requests also select an authorized role. No token,
LDAP password or private SSH key appears in the
schema. ReDoc loads its JavaScript and fonts from external CDNs, so the browser
needs internet access to render the page; downloaded schemas work offline.

For the Lima backend, management ports on the Linux host are not automatically
forwarded to the laptop. To view vendor-data and server access ReDoc, keep this tunnel open
in a terminal:

```bash
ssh -F "$HOME/.lima/latch-arm64/ssh.config" -N \
  -L 18300:127.0.0.1:8300 -L 18320:127.0.0.1:8320 lima-latch-arm64
```

Then open vendor data at <http://127.0.0.1:18300/docs> or server access at
<http://127.0.0.1:18320/docs>. If the instance uses a custom Lima home,
use its generated SSH configuration path instead. The optional device API has
no published host port; its documentation is available inside service management.
Do not publish management listeners to all interfaces to view documentation.

## Operations

| Method | Path | Behavior |
|---|---|---|
| GET | `/health/live` | Process liveness, independent of upstream services. |
| GET | `/health/ready` | Vendor-data readiness, including a valid OpenBao public SSH CA. |
| POST | `/v1/vendor-data` | Nova context in; a JSON string containing cloud-config out. |
| POST | `/v1/device-grants` | Authorize device/role, lease credentials and create an expiring Warpgate grant. |
| GET | `/v1/device-grants/{grant_id}` | Inspect an owned grant and reconcile its expiry. |
| DELETE | `/v1/device-grants/{grant_id}` | Revoke admission and sessions; retry dependency cleanup if needed. |
| POST | `/v1/server-grants` | Authorize an inventory server and coordinate Neutron access and certificate-backed Warpgate admission. |
| GET | `/v1/server-grants/{grant_id}` | Inspect an owned server grant and reconcile expiry. |
| DELETE | `/v1/server-grants/{grant_id}` | Revoke that grant's gateway access and release its network lease. |

A device grant defaults to five minutes and cannot exceed five minutes.
`cleanup_pending` means revocation is incomplete; the background reconciler
retries. A certificate or credential expiry alone does not terminate an active
SSH session. See [access workflows](../guides/access-workflows.md) for the
authentication, authorization and cleanup responsibilities.

Server grants also accept a TTL of 1–300 seconds and default to five minutes. The
request contains `server_id`, the laptop's `public_key` and optional
`ttl_seconds`; `Idempotency-Key` lets callers retry the same request safely.
The API returns a gateway address, gateway host key and `ssh_username` selector.
Warpgate obtains and presents the outbound SSH certificate; clients do not
receive a server private key or direct server access credential. Server requests use the
configured login account and do not currently implement the proposed server roles.
The deployed inventory adapter resolves `server_id` to an OpenStack instance;
physical-server inventory and network adapters remain future work.

## Regenerate the contract

Generate from application factories, without starting services or reading
deployment secrets:

```bash
.state/latch-api-venv/bin/python scripts/export_openapi.py
.state/latch-api-venv/bin/python scripts/export_openapi.py --check
```

The exporter writes readable YAML without an additional runtime dependency.
The drift check runs in the API documentation tests. Add new applications to
`application_schemas()` when their implemented routes become part of Latch.

The runtime documentation uses FastAPI's [ReDoc URL configuration](https://fastapi.tiangolo.com/tutorial/metadata/).
