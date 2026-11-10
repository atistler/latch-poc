# Warpgate migration

> Historical record. This document describes its original implementation stage. See the [current documentation](../README.md) for deployment and operating instructions.

The approved POC architecture replaces Boundary with a central, agentless SSH
gateway that records terminal sessions. The existing OpenStack VM, Neutron L3
forwarding, OpenBao CA and DynamicJSON vendor-data service remain in place.

Warpgate runs as a separate container. It uses provider address 100.64.10.10,
the address already allowed by the tenant security group. It has connected
service-management and control networks, no direct tenant route and IP
forwarding disabled. Laptop SSH and the HTTPS administration interface retain
loopback ports 9202 and 9200. The database and recordings use persistent storage.

The laptop authenticates to Warpgate using a local SSH key. Warpgate authorizes
the selected SSH target, requests a short-lived ubuntu certificate from OpenBao,
and establishes a separate SSH connection to 100.64.10.120:22001. Neutron forwards
that connection to the existing VM. The CA private key stays in OpenBao; the
gateway holds the session authentication key. Target host keys are verified.
Terminal recordings are enabled and their contents must be demonstrated by a
live connection and replay evidence.

The implementation pins a released version with OpenBao certificate support,
bootstraps reproducibly, and keeps credentials out of tracked files and command
arguments. Existing Boundary volumes are retained for rollback, but Boundary
services stop before Warpgate takes their provider address. The OpenStack disk,
tenant VM and OpenBao volume must not be recreated.

Acceptance covers SSH success, gateway authorization failure, certificate
policy, provider-source isolation, terminal recording, restart persistence and
access revocation. No agent is installed on the tenant.

## Live acceptance status: complete

On 2026-10-30, `./lab provision` retained primary VM `13fdcc3d-0b86-4cc7-a460-13143ea2267e` and the existing CA. `./lab verify` passed Neutron/network isolation, real gateway SSH, expected provider source and certificate-policy checks. It also decoded a terminal recording and found the verification output in recording `f4781163-da0e-4804-8f88-beafeadfc789`. Subsequent live checks passed unauthorized gateway-user rejection, recording persistence and SSH after container recreation, active-session termination with `./lab revoke`, rejection of new connections while revoked, and restored access with `./lab grant`. The original CA remained unchanged. The offline suite ran 74 tests with one integration-only skip.

See [validation evidence](warpgate-validation.md). List sessions with `./lab recordings`. Open [the administration UI](https://localhost:9200/@warpgate/admin) to inspect sessions and replay recordings; `/` returning HTTP 401 is expected.
