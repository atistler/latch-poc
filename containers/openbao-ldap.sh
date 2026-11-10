#!/bin/bash
set -euo pipefail
umask 077
mkdir -p /opt/latch/bao/storage
rm -f /opt/latch/bao/ldap-ready
node_id=latch-ldap-bao
if [[ -s /opt/latch/bao/init.json ]]; then
    # Raft membership is persistent identity: preserve the initialized node ID.
    [[ -s /opt/latch/bao/server.hcl ]] || { echo 'Initialized OpenBao config missing; refusing to replace Raft identity' >&2; exit 1; }
    node_id=$(awk -F '"' '/^[[:space:]]*node_id[[:space:]]*=/ { print $2 }' /opt/latch/bao/server.hcl)
    [[ "$node_id" =~ ^[A-Za-z0-9_-]+$ ]] || { echo 'Invalid existing Raft node identity' >&2; exit 1; }
fi
cat > /opt/latch/bao/server.hcl <<HCL
storage "raft" {
  path = "/opt/latch/bao/storage"
  node_id = "$node_id"
}
listener "tcp" {
  address = "0.0.0.0:8200"
  tls_disable = true
}
api_addr = "http://ldap-openbao:8200"
cluster_addr = "https://127.0.0.1:8201"
plugin_directory = "/opt/latch/plugins"
HCL
bao server -config=/opt/latch/bao/server.hcl &
pid=$!
trap 'kill "$pid" 2>/dev/null || true; wait "$pid" || true' EXIT
trap 'exit 0' TERM INT
bash /usr/local/lib/latch/bao-unseal.sh
python3 /usr/local/lib/latch/ldap-configure.py
wait "$pid"
