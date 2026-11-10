# Validation

Use [current validation status](status.md) to distinguish recorded evidence from
checks that still need to run. Commands below are procedures, not pass claims.
Run them from the repository root.

## Run checks


Run from the repository root in a virtual environment with
`python3 -m pip install -r latch_api/requirements.txt` installed:

```sh
python3 -m unittest discover -s tests -v
bash -n guest/*.sh containers/*.sh containers/openstack/entrypoint.sh tests/certificates-integration.sh
```

Offline tests check rendering, policy, API contracts, command construction and
recovery logic. API tests skip when their Python dependencies are absent;
recovery checks require `jq`. Inspect skips before treating a run as coverage.

| Check | Command or harness | Requires |
| --- | --- | --- |
| Real OpenBao/OpenSSH certificate policy | Build `tests/Dockerfile.certificates` and run its image | Docker; the harness enables the otherwise skipped certificate integration test |
| OpenStack tenant and certificate access | `./lab verify` | Provisioned cloud, tenant VM and core services |
| Real device SSH, command authorization and lifecycle | `./lab network-verify` | `./lab network-up`; invokes `tests/live_network_device.py` after foundation checks |
| API-managed server grant lifecycle | `.state/latch-api-venv/bin/python tests/live_server_gateway.py` | Provisioned Lima cloud and server API with no existing grants; creates grants and sessions and restarts/stops the server API to test recovery and expiry |
| Gateway control-plane contract | `python3 tests/live_device_gateway.py` | Configured Warpgate and local gateway key; creates and removes temporary target/user/role/pin objects without SSH or LDAP traffic |

Live checks can change access state or interrupt optional services. See the
[command reference](../reference/commands.md), [network acceptance checklist](network-device-checklist.md)
and current [network-device status](status.md#network-device-access).

## Certificate integration harness

Run the certificate integration independently on a Docker engine:

```sh
env -u DOCKER_DEFAULT_PLATFORM docker build -f tests/Dockerfile.certificates -t latch-certificate-test .
env -u DOCKER_DEFAULT_PLATFORM docker run --rm latch-certificate-test
```

## Recorded evidence

- [Current validation status](status.md): latest known coverage and outstanding checks.
- [Network-device acceptance checklist](network-device-checklist.md): required evidence for full acceptance.
- [October–November baseline](reports/2026-10-30-to-2026-11-04-baseline.md): server, certificate and vendor-data test history.
- [Network-device history](reports/network-device-history.md): earlier topology and policy results.
- [Archived Warpgate validation](../archive/warpgate-validation.md): migration-era evidence.

Historical results apply to the versions and topology tested. Reports under ignored
`.state/` and temporary logs are local artifacts and are not distributed with the repository.
