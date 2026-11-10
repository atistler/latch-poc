# Configuration sources

Files under `config/` are source-controlled templates and defaults. Live secrets, device
inventory, generated backend selection, and VM disks remain under
`.state/` or inside the lab VM; they are not configuration examples.

| Directory | Purpose and consumer |
| --- | --- |
| `config/openstack/` | DevStack base `local.conf`; guest installation and acceleration selection render the active DevStack configuration. |
| `config/network/` | FreeRtr hardware/software templates and TACACS+ command policy, copied into their container images. |
| `config/ldap/` | OpenBao dynamic-account creation/deletion LDIF templates. |
| `config/device/` | Initial device inventory copied only when private `.state/device/inventory.json` does not already exist. |

Template edits affect later builds, provisioning, or initial preparation.
Changing an example does not replace an existing live inventory or update a
running device.
