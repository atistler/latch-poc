# Infrastructure and access flows

[Documentation index](docs/README.md) · [Diagram sources](docs/architecture/README.md) · [Command reference](docs/reference/commands.md)

The default Lima backend runs OpenStack directly in a hardware-accelerated ARM64 Ubuntu Linux VM on the Mac. Controller, network-node and compute roles share that VM. The tenant is a second, nested ARM64 VM. Warpgate, OpenBao, the vendor-data API and the server grant API run in four separate containers after provisioning. The APIs have separate credentials and responsibilities. Warpgate replaces the former Boundary controller, worker and PostgreSQL services with an agentless SSH gateway and persistent terminal recordings.

The main diagrams show the core Warpgate topology. Gateway SSH, recording content/persistence, authorization rejection and revocation/restoration acceptance passed on 2026-10-30. Fresh OpenStack/Neutron, VM certificate access and recording checks passed on 2026-11-03; the rebuilt DynamicJSON API passed its renderer/readiness smoke test on 2026-11-02. The optional network-device topology is shown separately in the [network-device lab diagram](docs/guides/network-device-lab.md#logical-architecture): it adds OpenLDAP, LDAP OpenBao, the device API, freeRouter and tac_plus-ng on service management.

## Logical service networks

![Logical networks and access flows](docs/architecture/network-architecture.svg)

[Open full-size diagram](docs/architecture/network-architecture.svg) · [Editable source](docs/architecture/render_diagrams.py)

Blue solid arrows trace SSH traffic. Purple dotted arrows show trust and
credential flows; orange dashed arrows show control requests. Config-drive
delivery is a new-instance snapshot, rather than a tenant network connection.

This view excludes Lima transport, Docker bridges and other lab plumbing. Network boxes group logical roles, rather than physical hosts. Warpgate has interfaces on gateway ingress, service management and OpenStack external/provider networks. The user has no direct tenant route. The floating-IP endpoint is routable from Warpgate on the provider edge. Neutron routing and DNAT belong to OpenStack networking / tenant infrastructure and connect the external and tenant networks; the diagram does not place the routing agent process in the provider subnet. The logical routing function can use an L3 agent or OVN; the current POC uses an L3 agent, and OVN is not deployed or validated here.

| Network | Subnet | Members and routing |
| --- | --- | --- |
| Gateway ingress | `10.1.1.0/24` | Warpgate `10.1.1.2`; Docker gateway `.254` |
| Service management | `10.1.3.0/24` | Warpgate `.10`, TACACS+ `.21`, device-router `.253`; OpenBao, LDAP and APIs use dynamic service addresses; Docker gateway `.254` |
| Device management | `10.1.4.0/24` | freeRouter `.20`; device-router `.1` supplies routed SSH and TACACS+ connectivity without NAT |
| OpenStack management | `172.20.2.0/24` | Host `172.20.2.15` on persistent `latch-mgmt` dummy interface |
| OpenStack external/provider | `100.64.10.0/24` | Warpgate `.10`, diagnostic probe `.2`, OVS `br-ex` `.1/32`, floating IP `.120`, Docker bridge gateway `.254` |
| Tenant | `192.168.20.0/24` | Neutron router `.1`, tenant `.15`; SSH allowed only from provider `.10/32` |
| Lima transport | Lima-managed NAT | Internet egress and administrative host SSH; separate from service networks |

The client network is external context, not provisioned infrastructure. Device
management is a separate network for routed device SSH at `10.1.4.0/24`. LDAP, TACACS+ and LDAP OpenBao
are service-management services, not a separate AAA network. OpenStack host
management and Lima transport are deployment details rather than additional
access-pattern network types.

Gateway ingress is the new name for the existing `10.1.1.0/24` segment. This
documentation change preserves Compose identifiers and listener bindings,
including the lab's existing loopback publication of the Warpgate admin UI;
it does not migrate that listener onto service management.

The former database subnet `10.1.2.0/24` has no active service in this topology. Old Boundary database and worker volumes are retained for rollback. The previous `172.20.1.0/24` service-management subnet was replaced by `10.1.3.0/24` without changing the OpenBao CA.

Docker allocates provider addresses from `.0/27`, reserving `.1` for OpenStack. Neutron allocates `.100–.249`, avoiding overlap. Warpgate has connected ingress/management/provider routes, no default or direct tenant route, and `net.ipv4.ip_forward=0`. It opens new SSH connections rather than forwarding client IP packets.

## Local deployment

![ARM64 deployment and network boundaries](docs/architecture/architecture.svg)

[Open full-size diagram](docs/architecture/architecture.svg) · [Editable source](docs/architecture/render_diagrams.py)

This view shows the runtime boundaries and the physical bridge path. The
logical view above separates service networks from the Mac/Lima transport.

The Linux VM uses Apple VZ hardware virtualization. `LAB_ACCEL=auto` probes a real ARM KVM virtual CPU before enabling nested tenant acceleration. M1/M2 use ARM64 QEMU/TCG for the tenant. Supported M3-or-later systems with macOS 15+ may use KVM after the probe succeeds. `kvm` fails if unavailable; `tcg` forces software emulation. The lab has been tested on Apple M4, not physical M1/M2 hardware.

DevStack `stable/2025.2` supplies Keystone, Glance, Placement, Nova, Neutron, MySQL and RabbitMQ. The centralized `neutron-l3-agent` uses legacy mode and the port-forwarding extension. OVS/DHCP/metadata agents also run on the host. OVS supplies Layer 2; the L3 agent supplies routing and NAT in a Linux namespace. OVN, Horizon and Cinder are absent.

## SSH and certificate path

```mermaid
sequenceDiagram
    actor U as Laptop CLI and OpenSSH
    participant API as Latch server API
    participant O as Nova and Neutron APIs
    participant W as Warpgate SSH target
    participant B as OpenBao
    participant V as Tenant OpenSSH
    U->>API: server-grant with server UUID, public key and TTL
    API->>O: Verify inventory ownership and restricted network path
    API->>W: Isolated grant user, role, SSH target and host-key pin
    API-->>U: Grant login, expiry and gateway host-key pin
    U->>W: server-connect with grant ID through loopback 9202
    W->>W: Authorize expiring membership and start recording
    W->>B: Sign gateway key for ubuntu with 5-minute limit
    B-->>W: Signed user certificate
    W->>V: Verify VM host key and connect through Neutron DNAT
    V-->>U: Recorded ubuntu terminal through gateway
    API->>W: On revoke or expiry close sessions and remove admission
    API->>O: Remove owned forwarding after final grant
```

The laptop login key and outbound target key serve different authentication boundaries. The target key stays at Warpgate; the CA private key stays in OpenBao. OpenBao signs outside the terminal data path. The gateway uses a restricted signing role with principal `ubuntu`, bounded lifetime and session attribution. The separate operator signing role remains available for certificate-policy diagnostics.

Packets cross the Docker `latch-provider` bridge, `latch-docker`/`latch-ovs` veth pair, OVS `br-ex`, provider ports on `br-int`, and the router's `qg-*` interface. The router DNATs the destination through its `qr-*` tenant interface. Tenant-side `br-int` ports and Neutron filtering deliver to the VM. `br-int` is one integration bridge. A bridge-specific `DOCKER-USER` rule permits the local uplink.

During an active grant, the floating IP has a single TCP port-forwarding rule, not a full VM-port association. Latch validates the Nova instance, Neutron port/project ownership and gateway-only SSH rules before granting access. Concurrent grants share the network path and have separate gateway admission. The VM security group accepts TCP 22 only from `100.64.10.10/32`; an otherwise identical probe from `.2` must fail. Provider networking is private and not Internet-facing. `br-ex` uses `.1/32` to avoid a competing host `/24` route while retaining the Neutron gateway address.

Tenant OpenSSH trusts `/etc/ssh/latch-user-ca.pub`; authorized-key, password and keyboard-interactive login are disabled. The `latch-other` account tests wrong-principal rejection. No Warpgate agent runs on the tenant. Warpgate records terminal traffic because it terminates SSH and creates a new SSH connection to the target; this is a protocol-aware SSH target.

Certificate expiry blocks new authentication and does not end existing sessions. The server API persists grants in SQLite, closes grant sessions on revoke or expiry, removes per-grant admission and releases owned Neutron resources after the final grant. Disabled SSH targets can be retained until the last grant closes to preserve another grant's endpoint host-key pin; final cleanup collects them. Incomplete cleanup remains `cleanup_pending` and is retried after restart. An API outage can delay active-session termination. `./server-grant` issues a grant separately; `./server-connect --grant GRANT_UUID` requires that grant and leaves it active until revoke or expiry. The `grant` and `revoke` aliases manage the default/current grant. Current grants allow only the existing `ubuntu` login; VM role restrictions remain proposed. See the [access workflow](docs/guides/access-workflows.md#server-access).

Host Chrony serves provider `100.64.10.1:123/UDP`. Tenant systemd-timesyncd polls every 16–32 seconds through the Neutron router. The host follows upstream NTP with local stratum 10 fallback while offline. This prevents clock drift after laptop sleep from invalidating fresh certificates.

## Dynamic vendor data

Nova POSTs instance context to Latch API on Linux loopback `127.0.0.1:8300/v1/vendor-data`. Latch reads OpenBao's public SSH CA by service DNS, with no signing credential or access to CA storage. It renders certificate-only SSH policy, public CA, local NTP and a profile revision. Tenant metadata and user data do not select privileged platform policy.

Nova targets `cloud-init@http://127.0.0.1:8300/v1/vendor-data`. Latch returns a JSON string containing cloud-config; Nova writes the `cloud-init` wrapper into `openstack/latest/vendor_data2.json`. Both `nova-cpu.conf` and `nova.conf` need provider settings. `StaticJSON,DynamicJSON` with no static file path produces an empty legacy `vendor_data.json` required by this Nova config-drive builder. It supplies no fallback CA. Dynamic delivery is fatal on error, with bounded 3/5-second connect/read timeouts and a smaller OpenBao request budget.

New config drives snapshot configuration at instance creation. Reboot does not fetch an updated profile or CA. The retained primary VM has its original config drive. Fresh disposable builds validated DynamicJSON without user data, matching CA fingerprints, first-boot policy, certificate login/rejection, API/OpenBao outages, recovery and profile snapshot behavior. Evidence is in local `.state/dynamic-vm-validation.md`; disposable resources were cleaned up. [Cutover procedure](docs/guides/vendor-data-cutover.md) preserves the original tenant and disk for a separately reviewed replacement.

## Ports and persistent state

| Service | Laptop endpoint | Container / internal endpoint |
| --- | --- | --- |
| Warpgate administration and recordings | `https://localhost:9200/@warpgate/admin` | HTTPS `8888` |
| Warpgate SSH | `127.0.0.1:9202` | SSH `2222` |
| OpenBao | `http://127.0.0.1:8200` | HTTP `8200`; management service DNS |
| Latch vendor-data API | No laptop forward | Linux loopback `8300` → container `8300` |
| Latch server grant API | No laptop forward | Linux loopback `8320` → container `8320`; bearer authentication |
| Tenant SSH | No direct publication | Gateway → provider `.120:22001` → tenant `.15:22` |

Warpgate configuration, SQLite state, server keys and recordings use persistent storage. The server API keeps grant/cleanup state in its own persistent SQLite volume. Its trusted inventory, bearer token and OpenStack application credential are mounted separately; the vendor-data service cannot read them. ReDoc `/docs`, `/openapi.json` and `/openapi.yaml` are available on each API, with a generated combined [root contract](openapi.yaml). See [API access](docs/reference/api.md). OpenBao Raft/CA data stays in the existing `openbao` volume. OpenStack databases/images/VM disks stay on the Lima disk. Laptop keys and protected bootstrap state stay under `.state/lima/`, separate from legacy `.state/`. `down`/`up` preserves these; deleting the Lima VM destroys its disk and Docker volumes. Back up the disk and local state together. Never commit `.state/` or `.env`.

The retired Boundary volumes are retained, but Boundary must remain stopped before Warpgate claims provider `.10`. Do not run both gateways concurrently. This is separate from choosing the retained x86 backend.

This local POC uses self-signed Warpgate HTTPS and HTTP for OpenBao/Latch on trusted local networks. OpenBao automatically unseals with root-only recovery material in its own volume. Change-management integration and external identity-provider authentication are omitted. No paid Warpgate license is needed for SSH targets and local session recordings.

## Optional network-device access

![Temporary device credentials and recorded SSH](docs/architecture/device-access.svg)

[Open full-size diagram](docs/architecture/device-access.svg) · [Editable source](docs/architecture/render_diagrams.py) · [Policy and validation status](docs/guides/network-device-lab.md#validation-status)

The logical diagram places the device on a separate management network, with
routed SSH access from Warpgate and TACACS+ connectivity from the device to the
TACACS+ service on service management. The configuration uses freeRouter
`10.1.4.20`, Warpgate management `10.1.3.10` and TACACS+ `10.1.3.21`. A lab
`device-router` connects `10.1.3.253` to `10.1.4.1`, with static return routes
and no NAT; it forwards only gateway-to-device TCP 22, device-to-TACACS+ TCP 49
and replies. Live routed connectivity, isolation, roles, recordings and explicit
revocation passed; the complete suite stopped at its host-key test baseline. See
the [validation status](docs/guides/network-device-lab.md#validation-status). LDAP OpenBao issues temporary
device accounts; freeRouter delegates login and command authorization to
tac_plus-ng and OpenLDAP. It is separate from the core OpenBao SSH certificate
path. Warpgate records both access paths and checks the selected target host key.

## Verification and exports

The server naming and explicit-grant CLI update passed **197 offline tests:
196 passed and one integration-only test skipped** on 2026-11-04. A live smoke
test ran the exact root commands: `./server-grant`,
`./server-connect --grant GRANT_UUID` and `./server-revoke --grant GRANT_UUID`.
Actual SSH returned the server hostname, disconnecting left the grant active,
and explicit revoke closed it. The full renamed live acceptance suite then passed
at `2026-11-04T15:58:51Z` (`tests/live_server_gateway.py`). It verified API
credentials and inventory policy, two independent certificate-backed SSH grants
sharing one forwarding, API restart with live sessions retained, independent
revocation and reconnection with the surviving grant, replayable recordings,
final-grant network removal, normal active-session expiry and cleanup after a
grant expired during API downtime. Current evidence is in local
`.state/lima/server-verification.json`.

The earlier API-managed OpenStack server acceptance run passed at `2026-11-04T03:19:22Z`
(`tests/live_server_gateway.py`). It exercised rejected missing/incorrect bearer
credentials, unauthorized instances and destination/TTL overrides; two concurrent
certificate-backed SSH grants sharing one forwarding; API restart without losing
grants or sessions; independent revocation with the second grant still admitting
connections; final-grant network removal; replayable recordings; active expiry;
and startup cleanup after expiry during API downtime. Evidence is in local
`.state/lima/vm-verification.json`. The suite cleaned its own grants and network
access. It reset login-protection history only for its deliberately rejected test
identities; global protection remained enabled.

A final `./lab verify` passed ARM64 KVM, live Neutron agents, route isolation,
gateway-only provider access, real certificate SSH, signing-policy limits and
unsigned/wrong-principal/expired-certificate rejection. Recording
`b39f8b73-ae85-4d8a-a72f-73e37a79cc10` replay contained the verification marker.
That baseline offline suite ran **194 tests: 193 passed and one integration-only test
skipped**. Offline coverage includes wrong-pin rejection and cleanup-failure
recovery; the server live run did not repeat device-specific outage testing. Server role
restrictions remain a proposal.

The complete live `./lab network-verify` suite passed on 2026-11-03 local time (`2026-11-04T01:20:28Z`), with all ten checks passing: recorded readonly/operator/admin SSH and role permissions, explicit revoke and active expiry, host-key rejection/recovery, invalid/revoked LDAP credentials, and LDAP/TACACS+ outage fail-closed behavior with recovery. The earlier generic migration preserved existing credentials and data. Evidence is in `.state/lima/network-verification.json`; see the [network-device validation status](docs/guides/network-device-lab.md#validation-status) for recording IDs and precise role-test coverage. Gateway-restart replay passed independently for the prior run’s recordings; this latest suite did not repeat that restart check.

On 2026-11-03, fresh `./lab verify` passed after the generic device-access migration: live Neutron L3/OVS/DHCP agents, ARM64 KVM tenant, gateway route isolation, provider-source rejection, recorded VM SSH, signing limits and unsigned/wrong-principal/expired-certificate rejection. The VM observed gateway source `100.64.10.10`; recording `f61ab311-a4af-41a8-9588-0d318d259fb5` contained the verification marker and was replayable. The earlier live Latch API readiness/POST smoke test on 2026-11-02 returned the unchanged CA fingerprint and rendered CA trust, SSH policy, bootstrap metadata and NTP configuration through the shared renderer. This smoke test did not replace the existing tenant.

The rerun first cleared a pre-existing test-induced block for the lab NAT source `10.1.1.254` through Warpgate's supported admin API; login protection remained enabled with its existing policy. Network-device acceptance is tracked separately in the [current guide](docs/guides/network-device-lab.md#validation-status).

Run `./lab provision` and `./lab verify` after lifecycle/network changes. Gateway acceptance additionally requires a recorded marker visible in replay, unauthorized-user rejection, target host-key verification, retained recordings after restart and forced closure at revocation. A healthy container does not prove these outcomes. See [migration acceptance](docs/archive/warpgate-migration.md).

```sh
python3 docs/architecture/render_diagrams.py
```

`LAB_BACKEND=legacy` selects Docker Desktop/QEMU instead of Lima. Its runner boots an x86 guest, adding a runner bridge/TAP/provider NIC to the path. Provider `.2` is that runner address; on Lima `.2` is only the temporary diagnostic probe. Legacy host management uses QEMU SLIRP `172.20.2.15` with gateway `.2` and DNS `.3`. Its state and service volumes are separate. DynamicJSON is supported only in Lima; legacy does not deploy Latch API. Stop one backend before starting the other on the same laptop ports.

On 2026-10-30, `./lab provision` retained primary VM `13fdcc3d-0b86-4cc7-a460-13143ea2267e` and the existing CA. `./lab verify` passed Neutron/network isolation, real gateway SSH, expected provider source and certificate-policy checks. It also decoded a terminal recording and found the verification output in recording `f4781163-da0e-4804-8f88-beafeadfc789`. Subsequent live checks passed unauthorized gateway-user rejection, recording persistence and SSH after container recreation, active-session termination with `./lab revoke`, rejection of new connections while revoked, and restored access with `./lab grant`. The original CA remained unchanged. The offline suite ran 74 tests with one integration-only skip. Run `./lab recordings` to list sessions and use [the administration UI](https://localhost:9200/@warpgate/admin) for replay. The root URL returns HTTP 401 by design.

The [validation record](docs/archive/warpgate-validation.md) captures the completed migration acceptance checks.
