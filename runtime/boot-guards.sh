#!/bin/bash
set -euo pipefail
install -m 0644 /etc/codex-terminal/apparmor.profile /etc/apparmor.d/codex-terminal
apparmor_parser -r /etc/apparmor.d/codex-terminal
cat > /etc/systemd/system/codex-network-before-docker.service <<'UNIT'
[Unit]
Description=Block container access to Azure metadata before Docker starts
Before=docker.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh /etc/codex-terminal/network-rules.sh
[Install]
WantedBy=multi-user.target
UNIT
mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/codex-security.conf <<'UNIT'
[Unit]
Requires=codex-network-before-docker.service apparmor.service
After=codex-network-before-docker.service apparmor.service
UNIT
systemctl daemon-reload
systemctl enable codex-network-before-docker.service
sh /etc/codex-terminal/network-rules.sh
printf 'BOOT_GUARDS_INSTALLED\n'
