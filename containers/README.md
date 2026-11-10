# Container images and helpers

All service image definitions live here. The default Lima backend uses
[`compose.lima.yaml`](../compose.lima.yaml); optional LDAP/device services are
selected through its `device` and `network` profiles. Provisioning enables the
separate server grant API through the `servers` profile. The retained x86 backend uses
[`compose.legacy.yaml`](../compose.legacy.yaml).

| Image definition | Runtime helpers and responsibility |
| --- | --- |
| `Dockerfile.warpgate` | `warpgate.sh`: gateway setup/startup and restricted routing |
| `Dockerfile.openbao` | `openbao.sh`: persistent SSH-CA OpenBao |
| `Dockerfile.latch-api` | Vendor-data API from `latch_api/` and shared renderer/client in `latch_common/` |
| `Dockerfile.server-api` | server grant API with direct Nova/Neutron integration, certificate-backed Warpgate targets and persistent cleanup; the application drops privileges before serving requests |
| `Dockerfile.ldap` | `ldap.sh`: TLS-enabled OpenLDAP foundation |
| `Dockerfile.openbao-ldap` | `openbao-ldap.sh`: separate OpenBao instance with LDAP secrets plugin |
| `Dockerfile.device-api` | Device grant API from `latch_api/`, using the shared Warpgate client |
| `Dockerfile.tacacs` | `tacacs-start.py` and `tacacs-ldap.py`: tac_plus-ng configuration and LDAP authentication/health integration |
| `Dockerfile.freertr` | `freertr.sh`: native JVM freeRouter with persistent SSH identity |
| `Dockerfile.device-router` | `device-router.sh`: routes service management to device management with a default-deny forwarding policy and no forwarded NAT |
| `openstack/Dockerfile` | `openstack/entrypoint.sh`: legacy x86 QEMU runner; not used by default Lima OpenStack |

Core and optional service images use the repository root as their build context,
so their `COPY` instructions can include `config/`, API packages and helpers.
The legacy runner uses `containers/openstack/` as its build context.
Checked-in policy/templates belong in [`config/`](../config/); host installation
and tenant/network lifecycle scripts belong in [`guest/`](../guest/). Runtime
secrets and generated configuration belong in ignored `.state/` or service
volumes, not beside Dockerfiles.

freeRouter and tac_plus-ng are built from commit- and checksum-pinned source
archives. `freertr-timeouts.patch` is applied with no fuzz during the freeRouter
build and retained in the image for inspection. It bounds TCP connection setup
and TACACS+ command-response inactivity to five seconds each, so a missing
TACACS+ service rejects commands promptly. It preserves the device's
fail-closed authorization behavior. Review the patch against the selected source
revision when updating the pin; see the [network-device build notes](../docs/guides/network-device-lab.md#build-inputs).

See the [repository reference](../docs/reference/repository-layout.md#verification)
for offline and live verification. Building an image alone does not establish
SSH, LDAP, command authorization or recording acceptance.

`freertr-authorization.patch` labels configuration-mode TACACS+ requests with
`service=latch-config` while exec requests retain `service=shell`. The server
can then grant all actual configuration commands to an operator without
allowing ambiguous bare exec commands. Because configuration includes AAA,
operator access is not a strict security boundary from admin. The three-role policy passed complete live acceptance; see the
[validation guide](../docs/guides/network-device-lab.md#validation-status).
