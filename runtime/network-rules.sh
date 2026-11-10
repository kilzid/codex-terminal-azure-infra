#!/bin/sh
set -eu
iptables -N DOCKER-USER 2>/dev/null || true
iptables -C DOCKER-USER -d 169.254.169.254/32 -j REJECT 2>/dev/null || iptables -I DOCKER-USER 1 -d 169.254.169.254/32 -j REJECT
for port in 80 32526; do
  iptables -C DOCKER-USER -d 168.63.129.16/32 -p tcp --dport "$port" -j REJECT 2>/dev/null || iptables -I DOCKER-USER 1 -d 168.63.129.16/32 -p tcp --dport "$port" -j REJECT
done
