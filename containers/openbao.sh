#!/bin/bash
set -euo pipefail
umask 077
mkdir -p /opt/latch/bao/storage
cat > /opt/latch/bao/server.hcl <<'HCL'
storage "raft" {
  path = "/opt/latch/bao/storage"
  node_id = "latch-bao"
}
listener "tcp" {
  address = "0.0.0.0:8200"
  tls_disable = true
}
api_addr = "http://127.0.0.1:8200"
cluster_addr = "https://127.0.0.1:8201"
HCL
bao server -config=/opt/latch/bao/server.hcl &
pid=$!
trap 'kill "$pid" 2>/dev/null || true; wait "$pid" || true' EXIT
trap 'exit 0' TERM INT
bash /usr/local/lib/latch/bao-unseal.sh
bash /usr/local/lib/latch/bao-configure.sh
wait "$pid"
