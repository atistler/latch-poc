#!/bin/bash
set -euo pipefail
umask 077
mkdir -p /opt/latch/bao/storage /run/sshd
cat > /opt/latch/bao/server.hcl <<'EOF'
storage "raft" {
  path = "/opt/latch/bao/storage"
  node_id = "latch-bao"
}
listener "tcp" {
  address = "127.0.0.1:8200"
  tls_disable = true
}
api_addr = "http://127.0.0.1:8200"
cluster_addr = "https://127.0.0.1:8201"
EOF
bao server -config=/opt/latch/bao/server.hcl > /tmp/bao.log 2>&1 &
bao_pid=$!
trap 'kill "$bao_pid" 2>/dev/null || true' EXIT
bash guest/bao-unseal.sh
bash guest/bao-configure.sh
cp /opt/latch/bao/user-ca.pub /tmp/ca-before.pub
# Exercise persistence/unsealing across a real process restart.
kill "$bao_pid"
wait "$bao_pid" || true
bao server -config=/opt/latch/bao/server.hcl >> /tmp/bao.log 2>&1 &
bao_pid=$!
bash guest/bao-unseal.sh
bash guest/bao-configure.sh
cmp /tmp/ca-before.pub /opt/latch/bao/user-ca.pub
echo 'PASS: CA survives OpenBao restart and automatic lab unseal'
id ubuntu >/dev/null 2>&1 || useradd -m -s /bin/bash ubuntu
useradd -m -s /bin/bash latch-other
# Match Ubuntu cloud image accounts: password locked, SSH allowed through PAM.
passwd -l ubuntu
passwd -l latch-other
python3 - <<'PY'
import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location('target', 'guest/target-cloud-init.py')
target = importlib.util.module_from_spec(spec)
spec.loader.exec_module(target)
for item in target.cloud_config(Path('/opt/latch/bao/user-ca.pub').read_text())['write_files']:
    Path(item['path']).write_text(item['content'])
PY
sshd -t
/usr/sbin/sshd -p 2223 -E /tmp/sshd.log
LATCH_CERTIFICATE_INTEGRATION=1 python3 tests/test_certificates.py
