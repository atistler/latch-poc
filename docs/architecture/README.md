# Architecture diagrams

The [infrastructure overview](../../INFRASTRUCTURE.md) explains the core deployed
services, routing, certificate flow and vendor-data behavior.

| View | Diagram | Renderer function |
| --- | --- | --- |
| 01 · Local deployment: Mac, Lima, containers and nested VM | [architecture.svg](architecture.svg) | `deployment()` |
| 02 · Logical networks: server grants, SSH, certificate and vendor-data paths | [network-architecture.svg](network-architecture.svg) | `networks()` |
| 03 · Optional device access: LDAP leases and TACACS+ | [device-access.svg](device-access.svg) | `devices()` |

All three diagrams have one editable source:
[render_diagrams.py](render_diagrams.py). They describe the implemented access flows;
physical-server support and external identity/change-management integrations are
not depicted as deployed services.

Read the deployment view for runtime boundaries and the logical view for
service placement. Both show the separate server grant API coordinating gateway
admission and Neutron forwarding; it is outside the recorded SSH data path.
The logical view separates the routable floating-IP endpoint on the provider
edge from Neutron routing inside OpenStack networking / tenant infrastructure.
Routing is labeled L3 agent or OVN; the deployment view shows the current POC's
L3-agent implementation. These are logical boundaries, not agent process placement.
The optional device view adds freeRouter, tac_plus-ng,
OpenLDAP, LDAP OpenBao and the device API; it uses temporary passwords rather
than the VM path's SSH certificates. See the
[network-device guide](../guides/network-device-lab.md#logical-architecture)
for policy and current validation status.

The device view shows separate gateway and device management networks. Warpgate
uses management IP `10.1.3.10` to reach freeRouter at `10.1.4.20:22`; freeRouter
reaches TACACS+ at `10.1.3.21:49`. The lab device-router joins service management
at `10.1.3.253` and device management at `10.1.4.1`, without NAT. It permits only
these SSH/TACACS+ flows and replies. Live routed connectivity, isolation, role,
recording and explicit-revocation checks passed. The full suite stopped at its
host-key test baseline; see the [validation status](../guides/network-device-lab.md#validation-status).

## Network names

| Logical network | Purpose |
| --- | --- |
| Client network | User's source network; context, not provisioned by Latch. |
| Gateway ingress | Client SSH connectivity to Warpgate; lab `10.1.1.0/24`. |
| Service management | Latch APIs, gateway management, OpenBao, OpenLDAP and TACACS+; lab `10.1.3.0/24`. |
| OpenStack external/provider | Private reachability to floating-IP endpoints; lab `100.64.10.0/24`. |
| Tenant | OpenStack servers' private interfaces; lab `192.168.20.0/24`. |
| Device management | Device SSH management IPs reachable by routing from Warpgate; lab `10.1.4.0/24`, freeRouter `.20`. |

AAA services use service management. Transit describes connectivity, not another
network category. OpenStack host management and Lima transport appear only in
the deployment view as implementation details.

The network-name cleanup preserves listener bindings and Compose identifiers;
the device-subnet change adds routing and restricted forwarding separately. In particular, the lab still publishes
Warpgate's administration UI through its existing loopback/ingress binding;
the naming change does not move that listener onto service management.

The architecture infographics use numbered horizontal bands to separate access
requests, trust or credentials, and runtime connectivity. Orange dashed arrows
show control requests, purple dotted arrows show trust, credentials or AAA, and
blue solid arrows show SSH session traffic. Direct labels accompany the colors.
The logical network view leaves out Lima and Docker wiring; the deployment view
explains those lab boundaries. Dynamic service addresses are not invented.

Regenerate all SVGs from the repository root using Python 3 (standard library
only):

```sh
python3 docs/architecture/render_diagrams.py
```

The renderer validates each SVG as XML before writing it. The 1800 × 1290 SVGs
remain sharp when zoomed and load no external images or fonts. Review edits at
full size. For a PNG preview, install librsvg and run:

```sh
rsvg-convert -w 1800 docs/architecture/network-architecture.svg -o /tmp/latch-network.png
```

Commit the renderer and regenerated SVGs together. The former Graphviz sources
have been replaced by this renderer so there is one source of truth.
