# Latch POC implementation checklist

> Historical record. This document describes its original implementation stage. See the [current documentation](../../README.md) for deployment and operating instructions.

## Dynamic vendor data: complete

Live validation completed on 2026-10-30 using Boundary before the Warpgate migration. The primary VM and OpenBao CA were retained. Evidence: local `.state/dynamic-vm-validation.md`.

- [x] 0. Migrate management to `10.1.3.0/24`, gateway `.254`; preserve CA and rollback.
- [x] 1. Verify installed Nova/cloud-init and OpenBao contracts; capture fixtures.
- [x] 2. Build Latch API vendor-data/health routes, renderer and public-CA client; pass contract tests.
- [x] 3. Deploy Latch API and configure both Nova compute/metadata files, including empty StaticJSON compatibility entry; verify readiness/idempotence.
- [x] 4. Boot a disposable VM without user data; inspect config drive and prove CA trust, first-boot policy and certificate login/rejections.
- [x] 5. Prove fatal API/OpenBao failure behavior, existing-VM availability, recovery and configuration snapshots.
- [x] 6. Document findings and reversible [tenant cutover](../../guides/vendor-data-cutover.md); clean disposable resources and restore the profile. Primary-tenant replacement is not authorized or performed.

Full design: [plan.md](vendor-data-plan.md).

## Warpgate migration: complete

Design: [Warpgate migration](../warpgate-migration.md). Replace Boundary with a pinned Warpgate SSH gateway and terminal recordings; preserve the existing OpenBao CA, tenant disk and retired Boundary volumes. No target agent or paid gateway license.

- [x] Deploy separate Warpgate container and persistent state; stop Boundary before claiming provider `.10`.
- [x] Bootstrap laptop login, restricted OpenBao signing, trusted target host key and authorized SSH target reproducibly.
- [x] Prove real SSH login through Neutron, expected source `.10` and certificate restrictions.
- [x] Prove unauthorized gateway-user rejection.
- [x] Decode terminal recording and prove it contains the real verification output.
- [x] Prove recording persistence after restart.
- [x] Prove provider source isolation and unchanged primary VM/CA.
- [x] Prove active-session revocation and restoration.
- [x] Record final live evidence and update acceptance status in documentation.

Full [validation evidence](../warpgate-validation.md) includes container recreation, unauthorized-user rejection and revoke/grant. The offline suite ran 74 tests with one integration-only skip.

Live recording evidence: `f4781163-da0e-4804-8f88-beafeadfc789`; retained VM `13fdcc3d-0b86-4cc7-a460-13143ea2267e`. `./lab recordings` lists sessions.
