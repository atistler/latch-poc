#!/bin/sh
# Lab-only routed link between service management and device management.
set -eu
# Compose enables forwarding; readiness requires this default-deny policy.
# There is no NAT:
# TACACS+ must observe the actual device source for its device/shared-key policy.
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -A INPUT -i lo -j ACCEPT
iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A FORWARD -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A FORWARD -s 10.1.3.10 -d 10.1.4.20 -p tcp --dport 22 \
    -m conntrack --ctstate NEW -j ACCEPT
iptables -A FORWARD -s 10.1.4.20 -d 10.1.3.21 -p tcp --dport 49 \
    -m conntrack --ctstate NEW -j ACCEPT
while ip route del default 2>/dev/null; do :; done
exec sleep infinity
