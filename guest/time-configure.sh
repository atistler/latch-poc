#!/bin/bash
# The tenant has no Internet time source. Serve the host's clock on br-ex.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
if ! command -v chronyd >/dev/null; then
    apt-get update
    apt-get install -y chrony
fi
mkdir -p /etc/chrony/conf.d
cat > /etc/chrony/conf.d/latch.conf <<'EOF'
# Keep local time available to the isolated tenant while upstream is offline.
local stratum 10
bindaddress 100.64.10.1
allow 100.64.10.0/24
allow 192.168.20.0/24
EOF
# Ubuntu reads conf.d before its own makestep directive. Update that directive
# so a later default cannot restore the three-update limit after laptop sleep.
python3 - <<'PY'
from pathlib import Path
import re
path = Path('/etc/chrony/chrony.conf')
content = path.read_text()
content, count = re.subn(r'(?m)^makestep\s+.*$', 'makestep 1 -1', content)
if not count:
    content += '\nmakestep 1 -1\n'
path.write_text(content)
PY
mkdir -p /etc/systemd/system/chrony.service.d
cat > /etc/systemd/system/chrony.service.d/latch.conf <<'EOF'
[Unit]
After=latch-network.service
Wants=latch-network.service
EOF
systemctl daemon-reload
systemctl enable chrony
systemctl restart chrony
