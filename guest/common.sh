#!/bin/bash
# This file is sourced by root inside the disposable Linux guest.
# Shared values below are consumed by the scripts that source this file.
# shellcheck disable=SC2034
set -euo pipefail
BASE=/home/stack/latch
DATA=/opt/latch
mkdir -p "$DATA"
export PATH=/opt/stack/data/venv/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
FIP=100.64.10.120
WORKER=100.64.10.10
TENANT=192.168.20.15
os_auth() {
    # DevStack openrc references optional variables; it is not nounset-safe.
    set +u
    source /opt/stack/devstack/openrc admin admin >/dev/null
    set -u
}
wait_tcp() {
    local host=$1 port=$2
    for ((i=0; i<180; i++)); do
        if nc -z -w 2 "$host" "$port"; then return; fi
        sleep 2
    done
    echo "Timed out waiting for $host:$port" >&2
    return 1
}
