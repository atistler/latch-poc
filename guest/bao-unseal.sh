#!/bin/bash
set -euo pipefail
umask 077
export BAO_ADDR=http://127.0.0.1:8200
data=/opt/latch/bao
for ((i=0; i<60; i++)); do
    if curl -fsS "$BAO_ADDR/v1/sys/init" > "$data/init-status.json" 2>/dev/null; then break; fi
    sleep 1
done
initialized=$(jq -er '.initialized | tostring' "$data/init-status.json")
if [[ "$initialized" == false ]]; then
    bao operator init -key-shares=1 -key-threshold=1 -format=json > "$data/init.json.tmp"
    mv "$data/init.json.tmp" "$data/init.json"
fi
[[ -s "$data/init.json" ]] || { echo 'Missing lab OpenBao recovery material; refusing to reset CA' >&2; exit 1; }
# Feed recovery material on stdin; never place it in arguments or logs.
jq -ec '{key: .unseal_keys_b64[0]}' "$data/init.json" |
    curl -fsS -H 'Content-Type: application/json' --data-binary @- "$BAO_ADDR/v1/sys/unseal" |
    jq -e '.sealed == false' >/dev/null
