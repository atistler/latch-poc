#!/bin/sh
set -eu
umask 077
state=/var/lib/freertr
secret_file=/run/network-secrets/tacacs-secret
mkdir -p "$state" /run/freertr
# Host-OS sockets back the SSH/TACACS+ services. Return traffic must use the
# routed device uplink rather than Docker's bridge gateway.
if [ -n "${LATCH_MANAGEMENT_NEXT_HOP:-}" ]; then
    while ip route del default 2>/dev/null; do :; done
    ip route replace 10.1.3.0/24 via "$LATCH_MANAGEMENT_NEXT_HOP"
fi
test -s "$secret_file" || { echo 'Missing TACACS shared secret' >&2; exit 1; }
secret=$(cat "$secret_file")
# Keep generated router configuration unambiguous: the provisioner uses hex.
case "$secret" in ''|*[!a-zA-Z0-9]*) echo 'TACACS secret must be alphanumeric' >&2; exit 1;; esac
address=$(getent ahostsv4 "${TACACS_HOST:-tacacs}" | awk 'NR==1 {print $1}')
test -n "$address" || { echo 'Cannot resolve TACACS service' >&2; exit 1; }
if [ ! -s "$state/hostkey.pem" ]; then
    ssh-keygen -q -t rsa -b 3072 -m PEM -N '' -f "$state/hostkey.pem"
fi
# Export only the public key for inventory pinning; retain the private key on volume.
ssh-keygen -y -f "$state/hostkey.pem" > "$state/hostkey.pub"
chmod 644 "$state/hostkey.pub"
sed -e "s/@TACACS_ADDRESS@/$address/g" -e "s/@TACACS_SECRET@/$secret/g" \
    /opt/freertr/router-sw.template > /run/freertr/router-sw.txt
cp /opt/freertr/router-hw.txt /run/freertr/router-hw.txt
exec java -Xms32m -Xmx256m -Djava.awt.headless=true -jar /opt/freertr/freertr.jar router /run/freertr/router-
