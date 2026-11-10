#!/bin/bash
set -euo pipefail
DATA=/opt/latch
umask 077
export BAO_ADDR=http://127.0.0.1:8200
export BAO_TOKEN
BAO_TOKEN=$(jq -er .root_token "$DATA/bao/init.json")
if ! bao secrets list -format=json | jq -e 'has("ssh-client-signer/")' >/dev/null; then
    bao secrets enable -path=ssh-client-signer ssh
fi
if ! bao read -format=json ssh-client-signer/config/ca >/dev/null 2>&1; then
    bao write ssh-client-signer/config/ca generate_signing_key=true key_type=ed25519 >/dev/null
fi
bao write ssh-client-signer/roles/latch-user - <<'EOF'
{
  "key_type": "ca",
  "allow_user_certificates": true,
  "allow_host_certificates": false,
  "allowed_users": "ubuntu",
  "default_user": "ubuntu",
  "ttl": "15m",
  "max_ttl": "15m",
  "allowed_extensions": "permit-pty",
  "default_extensions": {"permit-pty": ""}
}
EOF
bao policy write latch-sign - <<'EOF'
path "ssh-client-signer/sign/latch-user" { capabilities = ["update"] }
EOF
bao write ssh-client-signer/roles/latch-warpgate - <<'EOF'
{
  "key_type": "ca",
  "allow_user_certificates": true,
  "allow_host_certificates": false,
  "allow_user_key_ids": true,
  "allowed_users": "ubuntu",
  "default_user": "ubuntu",
  "ttl": "5m",
  "max_ttl": "5m",
  "allowed_extensions": "permit-pty",
  "default_extensions": {"permit-pty": ""}
}
EOF
bao policy write latch-warpgate - <<'EOF'
path "ssh-client-signer/sign/latch-warpgate" { capabilities = ["update"] }
EOF
if ! bao auth list -format=json | jq -e 'has("approle/")' >/dev/null; then
    bao auth enable approle
fi
# Gateway tokens can sign ubuntu certificates only. The POC stores its AppRole
# secret ID in protected gateway state; no root token is given to Warpgate.
bao write auth/approle/role/latch-warpgate token_policies=latch-warpgate \
    token_ttl=15m token_max_ttl=1h secret_id_num_uses=0 >/dev/null
if ! bao auth list -format=json | jq -e 'has("userpass/")' >/dev/null; then
    bao auth enable userpass
fi
# Explicit local identity for certificate policy diagnostics.
bao write auth/userpass/users/latch password=latch-local-only token_policies=latch-sign \
    token_no_default_policy=true token_ttl=15m token_max_ttl=15m >/dev/null
bao read -field=public_key ssh-client-signer/config/ca > "$DATA/bao/user-ca.pub"
echo 'OpenBao SSH CA ready (persistent local lab storage).'
