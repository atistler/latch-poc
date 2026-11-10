# Current validation status

This page summarizes the latest recorded evidence as of 2026-11-10. The documentation
reorganization did not rerun live acceptance. Historical reports retain older results;
they do not establish acceptance of the current topology.

## Offline checks

The last recorded suite ran 199 tests: 198 passed and one certificate integration
test was skipped. That test requires its real OpenBao/OpenSSH container harness.
See [validation procedures](README.md) for commands and prerequisites.

## Server access

The post-routing `./lab verify` regression passed ARM64 KVM, live Neutron forwarding,
network isolation, gateway-only provider access, valid SSH certificates, signing
limits, and rejection of unsigned, wrong-principal and expired credentials. A real
SSH session produced decoded recording `be796c9b-7122-48cd-9edf-9c0d02312bda`.
The local log was `/tmp/latch-routed-server-verify.log`.

The full API-managed server lifecycle last passed at **2026-11-04T15:58:51Z**, before
the device routing changes. It covered concurrent grants and shared forwarding,
independent revocation, API restart recovery, recordings, final-grant cleanup,
active expiry and reconciliation after API outage. Its local report is
`.state/lima/server-verification.json`. This full harness has not been rerun for
the routed-device topology.

## Network-device access

**Partial acceptance; the full routed-device suite has not passed.**

The latest routed run passed:

- Route/interface checks and default-deny forwarding without forwarded NAT.
- Warpgate to device SSH, device to TACACS+, and TACACS+ to LDAP connectivity.
- Denial of TACACS+ to device SSH and device to LDAP traffic.
- Dynamic LDAP issuance, bind, group membership, ACL and revocation for all three roles.
- Real readonly/operator/admin SSH command policy and decoded recordings.
- Explicit revocation closing an established session and rejecting reconnection.

The suite stopped with `Host-key test baseline did not execute a real router command`.
This is an incomplete test baseline, not evidence that host-key enforcement failed.
Host-key rejection/recovery, active expiry and LDAP/TACACS+ outage checks remain
unvalidated on this routed topology. The local log is `/tmp/latch-precommit-network.log`;
older `.state/lima/network-verification.json` results must not be treated as this run.

See the [acceptance checklist](network-device-checklist.md) for required coverage and
[historical device results](reports/network-device-history.md) for earlier runs.
Operators can change AAA configuration, so operator is not a strict security
boundary from admin.

## Provisioning and platform limits

The disposable-VM Nova DynamicJSON test passed on 2026-10-30; renderer/readiness smoke
checks were recorded on 2026-11-02. See the [baseline report](reports/2026-10-30-to-2026-11-04-baseline.md).
These checks were not rerun during this documentation work.

Physical M1/M2 hardware has not been tested; the recorded accelerated runs used an
Apple M4. M1/M2 tenant emulation remains a supported configuration path with this
validation limitation. OVN is an architectural alternative, not the deployed backend.
Physical-server adapters and [server roles](../design/server-roles.md) remain unimplemented;
the current server login is `ubuntu`.
