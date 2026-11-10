#!/usr/bin/env python3
"""Emit Nova StaticJSON vendor data containing cloud-init platform defaults."""
import json
from pathlib import Path
import sys

# This script also runs directly inside the guest, outside a Python install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from latch_common.cloud_init import cloud_config


if __name__ == '__main__':
    config = '#cloud-config\n' + json.dumps(cloud_config(Path(sys.argv[1]).read_text()), indent=2)
    print(json.dumps({'cloud-init': config}, indent=2))
