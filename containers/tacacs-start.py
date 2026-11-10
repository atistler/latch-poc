#!/usr/bin/env python3
"""Render secret to a private runtime config, validate, and start foreground."""
import ipaddress
import os
from pathlib import Path
import re
import subprocess

os.umask(0o077)
if next_hop := os.environ.get("LATCH_DEVICE_NEXT_HOP"):
    subprocess.run(["ip", "route", "replace", "10.1.4.0/24", "via",
                    str(ipaddress.ip_address(next_hop))], check=True)
secret = Path("/run/network-secrets/tacacs-secret").read_text().strip()
if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", secret):
    raise SystemExit("TACACS secret must be 32-128 URL-safe characters")
runtime = Path("/run/tacacs.cfg")
address = str(ipaddress.ip_address(os.environ.get("LATCH_FREERTR_ADDRESS", "10.1.4.20")))
runtime.write_text(Path("/etc/latch/tacacs.cfg").read_text().replace("@TACACS_SECRET@", secret).replace("@FREERTR_ADDRESS@", address))
subprocess.run(["/usr/local/sbin/tac_plus-ng", "-P", str(runtime)], check=True)
os.execv("/usr/local/sbin/tac_plus-ng", ["tac_plus-ng", str(runtime)])
