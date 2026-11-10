# Network-device validation history

These are dated observations from earlier runs, retained verbatim from the operating
guide. They are not a statement that every check passes on the current topology.
See the [current validation status](../status.md) for the latest result and limitations.

On 2026-11-10, the routed-subnet run passed explicit route and interface checks,
allowed SSH/TACACS+ flows, blocked cross-network access, source preservation,
all three roles, decoded recordings and explicit revocation with reconnect
rejection. Docker's loopback DNS translation is excluded from the forwarded-NAT
check; regression tests still reject NAT on device traffic. The complete suite
stopped because its host-key baseline did not execute a real router command.
Host-key rejection/recovery, expiry and outage checks therefore remain
unvalidated on this topology. The last complete passing report below is
historical and uses the former shared management subnet.

The pre-commit offline suite ran 199 tests: 198 passed and one integration-only
test skipped. The post-routing `./lab verify` also passed the OpenStack server
path, certificate rejection checks and replayable session recording.

The complete live `./lab network-verify` suite passed on 2026-11-03 local time
(report timestamp `2026-11-04T01:20:28Z`). All ten checks passed against the
running services, including all three roles:

- Read-only SSH permitted `show running-config`, admitted the ping diagnostic,
  and reached a real TCP listener through `telnet`, with its banner observed.
  The router has no data-plane interface for this ping probe, so this proves
  command admission rather than successful ICMP delivery. Configuration, save,
  sleep and output-redirection probes were denied.
- Operator and admin sessions applied arbitrary description text, verified the
  effective configuration through interface output, saved with `write memory`
  and restored the test change. `sleep` was denied for operator and permitted
  for admin. All three role sessions produced decoded recordings and were
  revoked afterward.
- Explicit revoke closed active SSH and rejected reconnect. A 30-second grant
  expired, closed active SSH and rejected reconnect.
- A wrong device host-key pin rejected SSH; restoring the pin restored access.
  Wrong LDAP passwords and revoked LDAP identities were rejected.
- TACACS+ and LDAP outages each denied commands in existing sessions and new
  login attempts. Restoring each service restored access.

Decoded recordings contained the actual session output:

| Role | Recording ID |
| --- | --- |
| `readonly` | `e5305fa3-70bc-402b-8730-5d1881b33c94` |
| `operator` | `d0c44906-4019-4fcc-a6ef-db0723fa8db8` |
| `admin` | `ce931076-f171-426c-bbc7-ae9ea00ef934` |

The local report is `.state/lima/network-verification.json`. Independently,
fresh `./lab verify` passed the existing OpenStack VM path on 2026-11-03, with
replayable recording `f61ab311-a4af-41a8-9588-0d318d259fb5`. The offline suite ran
130 tests with one integration-only skip.

Deliberate rejected-login probes share lab NAT source `10.1.1.254`. The harness
clears only that test source's failure history/block through the supported
Warpgate admin API around those probes; it leaves the enabled login-protection
policy unchanged and records these cleanup operations in the report. This
prevents expected negative tests from blocking subsequent valid test users.

Gateway-restart persistence passed independently for the prior run: recordings
`f4635e06-199c-4c2c-8995-d38dc2af51fd` and
`a96389d1-6a85-445f-9d47-2cb546fdc8c3` remained replayable with their CLI output
after an actual Warpgate restart. The latest three-role suite did not repeat
that restart check.
