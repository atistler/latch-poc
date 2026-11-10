# Warpgate migration validation

> Historical record. This document describes its original implementation stage. See the [current documentation](../README.md) for deployment and operating instructions.

Acceptance completed on 2026-10-30 on the existing Apple M4 / ARM64 Lima lab, using Warpgate 0.30.2 and the existing OpenBao 2.7.2 CA. No target agent or paid gateway license was added. The optional LDAP/device path was not deployed at that stage.

| Check | Observed result |
| --- | --- |
| Startup and provisioning | `./lab up` completed with healthy services; `./lab provision` retained primary VM `13fdcc3d-0b86-4cc7-a460-13143ea2267e` |
| Real SSH target | `./lab verify` connected through Warpgate and Neutron forwarding; VM observed provider source `100.64.10.10` |
| Network and certificate policy | Neutron agents, route isolation, rejected provider source `.2`, certificate signing restrictions and rejected invalid certificate authentication passed |
| Recording contents | Verification decoded recording `f4781163-da0e-4804-8f88-beafeadfc789` and found actual terminal output marker `latch-recording-1793374533034102000` |
| Container recreation | The same recording remained retrievable after recreating Warpgate; a fresh SSH connection succeeded |
| Gateway authentication rejection | `unauthorized:latch-vm` was rejected with `Permission denied` |
| Active-session revocation | A confirmed active SSH session running `sleep 300` terminated within the validation's 20-second deadline after `./lab revoke` |
| New-session revocation | New SSH failed while forwarding was revoked |
| Restoration | `./lab grant` restored forwarding and a fresh SSH login succeeded |
| CA preservation | SHA-256 of the stripped public-key endpoint response remained `8accf318fd456aebbb63b80071d5c0e90a957c13dc8ce24be873b4134348af67` |
| Administration readiness | `/@warpgate/admin` returned HTTP 200; HTTP 401 at `/` is expected |
| Offline checks | 74 unit tests ran with one integration-only skip |

The endpoint-response digest above hashes the textual SSH public-key response. It differs from `ssh-keygen`'s key fingerprint; the retained CA fingerprint is `SHA256:FLZ2zQC5ICf8oDYnHMqMU2nPurJD6OaISF3SWXsbp6I`.

Local supplementary checks ran from `.state/warpgate-validation.py`; generated state, private keys and credentials remain outside version control. Recording evidence proves captured output and replay data availability. Container recreation was tested; this migration did not repeat a complete macOS/Lima reboot or establish production hardening.

```sh
./lab connect
./lab recordings
```

Use [Warpgate administration](https://localhost:9200/@warpgate/admin) to inspect sessions and replay recordings. Sign in as `admin` using the generated password in `.state/lima/warpgate/admin-password`; the local interface uses self-signed TLS. The [architecture guide](../../INFRASTRUCTURE.md) describes network boundaries and [migration design](warpgate-migration.md) documents preserved state and rollback constraints.
