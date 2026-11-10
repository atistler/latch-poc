# Server access

Run commands from the repository root after completing [getting started](../getting-started.md).
The implemented adapter supports OpenStack VMs. Physical-server adapters remain future work.

## Grants and revocation

```sh
./server-grant --ttl 300
./server-status
./server-connect --grant GRANT_UUID
./server-revoke --grant GRANT_UUID
```

Replace `GRANT_UUID` with the ID printed by `server-grant`. Add
`--server SERVER_UUID` when selecting among authorized inventory servers.
`./lab grant` creates the default grant; `./lab revoke` revokes the current grant.
Without `--grant`, `server-status` and `server-revoke` use that remembered current grant.
`./server-connect` requires `--grant`; it never creates a grant. Disconnecting
does not revoke it. Root commands also work as `./lab server-grant`,
`./lab server-connect`, `./lab server-status` and `./lab server-revoke`.

The API closes a revoked or expired grant's active sessions and removes its
gateway admission. Disabled targets can remain until the final grant closes
so another grant keeps its shared endpoint host-key pin. It releases its owned
Neutron forwarding only after the
last grant for that VM is cleaned up. Durable cleanup retries survive API
restart; `cleanup_pending` means closure has not completed. Expiring membership
blocks new gateway authorization, but active termination requires the API
worker to run. Server readonly/operator/admin roles remain proposed; current server
grants provide the existing `ubuntu` shell.

See the [full server access workflow](access-workflows.md#server-access),
[command reference](../reference/commands.md#server-grants) and
[API reference with ReDoc access](../reference/api.md).

## SSH certificates and target trust

OpenBao 2.7.2 stores its Raft data and SSH CA in the Linux VM's `openbao` Docker volume. The laptop key authenticates to Warpgate, independently of the target VM. Warpgate requests a five-minute `ubuntu` certificate from OpenBao for its outbound SSH connection. The target authentication key stays at the gateway; the CA private key stays in OpenBao. Warpgate verifies the VM's SSH host key and records terminal sessions in persistent storage. The laptop key is `.state/lima/gateway_ed25519`; Latch creates a unique Warpgate user, role and SSH target for each server grant. View sessions and replay recordings in the HTTPS administration interface at `https://localhost:9200/@warpgate/admin` (local self-signed TLS).

```sh
./lab recordings  # list recorded sessions; replay from the administration UI
```

The administration UI path is `/@warpgate/admin`; an HTTP 401 at `/` is expected.

`./lab certificate` remains an operator inspection command for the separate restricted lab signing role. Its certificates are not how the laptop authenticates to Warpgate.

New Lima tenants receive platform configuration from Latch API through Nova DynamicJSON. Nova sends instance context to `POST /v1/vendor-data`; Latch reads OpenBao's public SSH CA endpoint without a signing token and returns cloud-config for CA trust, SSH policy and local NTP. Nova writes `openstack/latest/vendor_data2.json` into the config drive. No user data or CA private key is sent to the VM. Existing VMs retain their original config drive; provisioning does not replace them or refresh configuration on ordinary reboot. The [cutover and recovery procedure](vendor-data-cutover.md) describes a separately approved replacement while retaining the original VM and disk.

The tenant has 1 GiB RAM and an 8 GB disk. Cloud-init installs `TrustedUserCAKeys` and disables authorized-key, password and keyboard-interactive login. The locked-password `latch-other` account exists only to test principal mismatch; the signing role allows only `ubuntu`.

Development credentials:

| Service | Credentials |
| --- | --- |
| Warpgate, `https://localhost:9200/@warpgate/admin` | `admin`; generated password in `.state/lima/warpgate/admin-password` (Lima backend) |
| OpenBao, `http://127.0.0.1:8200` | Userpass `latch` / `latch-local-only` |
| OpenStack | Admin password `latch-openstack` |

The operator signing role permits `ubuntu` certificates with a 15-minute maximum. The gateway uses a separate restricted signing identity/role for five-minute target certificates and session attribution. OpenBao signing permission and gateway target authorization are distinct controls.

**Local lab only:** OpenBao and Latch use HTTP on trusted local networks; Warpgate administration uses self-signed HTTPS. OpenBao automatically unseals using root-only recovery material in `/opt/latch/bao/init.json` in its persistent volume. This is local recovery convenience, not production secret custody. Certificate expiry prevents new logins; it does not terminate established sessions.

## Related documentation

- [End-to-end access workflow](access-workflows.md#server-access).
- [Proposed server roles](../design/server-roles.md), not implemented.
- [Current validation status](../validation/status.md#server-access).
