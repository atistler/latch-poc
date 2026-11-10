# Network-device acceptance checklist

This checklist defines required evidence for Warpgate → freeRouter SSH →
tac_plus-ng → OpenLDAP, with temporary LDAP accounts issued by OpenBao. It is
an acceptance contract, not a passing test report. See the [current validation
status](status.md) for which checks passed on the deployed topology and what
remains unvalidated. [Historical reports](reports/network-device-history.md)
retain earlier run details and recording IDs.

Run `./lab network-up`, then `./lab network-verify` against the deployed Lima
lab. The verifier exercises real services and temporarily interrupts LDAP and
TACACS+; run it when device sessions may be disrupted.

| Area | Required evidence |
| --- | --- |
| LDAP lifecycle | Unique leased accounts, role groups, password authentication, expiry and explicit revocation |
| Gateway access | Real SSH through Warpgate with a pinned device host key and no direct device host port |
| Command policy | Read-only running configuration, diagnostics and outbound connections succeed while configuration/save/reload/files/shell and output redirect/pastebin/record options fail; operator configuration and saves succeed while standalone sleep/reload/files/shell fail; an admin-only harmless command succeeds |
| Fail closed | LDAP and TACACS+ outages reject new authentication/authorization; restored services recover |
| Grant lifecycle | Revoke and expiry reject new access and terminate managed active sessions |
| Session evidence | Recorded session output contains a known marker; recordings remain readable after gateway restart |
| Persistence | Device host key, trusted inventory and existing grants survive appropriate service restarts |
| Existing VM path | `./lab verify` still passes Neutron isolation, certificate policy and recorded server access |

A healthy container or passing offline tests alone does not establish these
outcomes. Review the verifier output and recording evidence before marking a
live run complete.

The original agent task/ownership log is retained in the
[implementation archive](../archive/plans/network-open-source-implementation.md).
