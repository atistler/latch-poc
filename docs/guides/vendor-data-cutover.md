# Vendor-data cutover and recovery

The current tenant retains its existing disk and config drive. Rebooting it does not refresh vendor data. Replacing the primary tenant or switching its forwarding requires a separate cutover decision; the steps below prepare that decision.

## Validate before switching

1. Record the original server UUID, Neutron port, floating-IP port-forwarding ID and Warpgate target. Retain the original server and disk throughout validation and rollback.
2. Confirm Latch readiness and the expected OpenBao public CA fingerprint. Check the effective configuration of both Nova compute and metadata services; DevStack can use different configuration files for each service.
3. Create a replacement with a different name and fixed address, config drive enabled and no user data. Use the same approved image, flavor and gateway-only SSH security policy. Do not use rebuild on the original server.
4. Inspect the replacement config drive: `openstack/latest/vendor_data2.json` must include the `cloud-init` target response, CA trust and expected bootstrap revision. Confirm no tenant user-data payload exists.
5. Assign a separate external forwarding port and temporary Warpgate target. Verify cloud-init completion, certificate-only SSH, time synchronization and the expected gateway source address. Reject unsigned, wrong-principal and expired certificates.
6. Pin the replacement host key in the temporary Warpgate target; use separate laptop host-key state if bypassing the gateway for diagnostics. Record its host-key fingerprint through the hypervisor console or another trusted channel before promoting it; do not silently delete an existing host-key warning.

## Switch after approval

Record the exact old forwarding destination. Stop new access during the brief switch, update the primary forwarding destination to the validated replacement port, and update the gateway target host-key pin to the verified replacement key before testing the primary Warpgate SSH target with a fresh gateway-issued certificate. Handle the changed host key using the verified replacement fingerprint. Keep the original VM and its port available until the agreed retention period ends. Existing sessions may survive a forwarding change and should be considered explicitly during the cutover window.

## Roll back

Restore the recorded primary forwarding destination to the original Neutron port and verify its original host key and certificate login. Remove only the temporary resources created for the replacement after confirming primary access. Do not delete the original VM, volume or OpenBao data during recovery.

For a Nova provider configuration rollback, restore each service's protected pre-change configuration from `/opt/latch/config-backup/` to its matching original configuration file and restart only the affected services. Verify the service command line/configuration path rather than assuming every Nova service reads `nova.conf`. Restoring provider configuration affects future metadata/config-drive generation; it does not rewrite an already created config drive.

For management-network rollback, use the protected migration backup directory recorded in `.state/dynamic-live-validation.md`. Its saved Compose configuration and network inventory describe the original subnet. Restore service attachments without removing the persistent OpenBao volume, then compare the public CA fingerprint and verify Warpgate connectivity.
