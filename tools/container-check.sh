#!/bin/bash
set -euo pipefail
check_root=$(mktemp -d)
trap 'sudo rm -rf "$check_root"' EXIT
sudo apt-get update -qq >/dev/null
sudo apt-get install -y -qq apparmor apparmor-utils >/dev/null
sudo apparmor_parser -r runtime/apparmor.profile
mkdir -p "$check_root/workspace" "$check_root/config"
printf '%s\n' 'sandbox_mode = "workspace-write"' 'approval_policy = "on-request"' > "$check_root/config/config.toml"
sudo chown -R 1000:1000 "$check_root"
image=$(python3 -c 'import json;print(json.load(open("images.lock.json"))["terminal"])')
docker run --rm --user 1000:1000 --cap-drop ALL --pids-limit 512 \
  --memory 1280m --memory-swap 1280m --cpus 0.9 \
  --security-opt no-new-privileges:true \
  --security-opt "seccomp=$PWD/runtime/seccomp.json" --security-opt apparmor=codex-terminal \
  -v "$check_root/workspace:/workspace" -v "$check_root/config:/home/node/.codex" \
  -v "$PWD/runtime/sandbox-check.sh:/tmp/sandbox-check.sh:ro" \
  -v "$PWD/tests/native-smoke.mjs:/tmp/smoke.mjs:ro" \
  "$image" sh -ceu 'sh /tmp/sandbox-check.sh; node /tmp/smoke.mjs'
python3 tools/proxy-check.py
