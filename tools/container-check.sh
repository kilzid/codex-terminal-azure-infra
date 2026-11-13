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
# Require the backend used by production before running recovery tests.
docker run --rm --user 1000:1000 --cap-drop ALL --network none \
  --security-opt no-new-privileges:true --entrypoint sh "$image" -ceu '
    if ! command -v herdr >/dev/null 2>&1; then
      echo "INCOMPATIBLE_TERMINAL_IMAGE: Herdr is required by session recovery and deployment verification." >&2
      exit 1
    fi
  '
docker run --rm --user 1000:1000 --cap-drop ALL --pids-limit 512 \
  --memory 1280m --memory-swap 1280m --cpus 0.9 \
  --security-opt no-new-privileges:true \
  --security-opt "seccomp=$PWD/runtime/seccomp.json" --security-opt apparmor=codex-terminal \
  -v "$check_root/workspace:/workspace" -v "$check_root/config:/home/node/.codex" \
  -v "$PWD/runtime/sandbox-check.sh:/tmp/sandbox-check.sh:ro" \
  -v "$PWD/runtime/session-manager.mjs:/opt/codex-session.mjs:ro" \
  -v "$PWD/runtime/session-hooks.toml:/etc/codex/requirements.toml:ro" \
  -e SESSION_LAUNCHER=/opt/codex-session.mjs \
  -v "$PWD/runtime/native-probe.mjs:/tmp/native-probe.mjs:ro" \
  -v "$PWD/tests/native-smoke.mjs:/tmp/smoke.mjs:ro" \
  "$image" sh -ceu 'sh /tmp/sandbox-check.sh; node /tmp/smoke.mjs'
# Real container replacement: the second run has no prior processes or Herdr server.
for phase in prepare verify; do
  docker run --rm --user 1000:1000 --cap-drop ALL --pids-limit 512 \
    --memory 1280m --memory-swap 1280m --cpus 0.9 \
    --security-opt no-new-privileges:true \
    --security-opt "seccomp=$PWD/runtime/seccomp.json" --security-opt apparmor=codex-terminal \
    -v "$check_root/workspace:/workspace" -v "$check_root/config:/home/node/.codex" \
    -v "$PWD/runtime/session-manager.mjs:/opt/codex-session.mjs:ro" \
    -v "$PWD/runtime/session-hooks.toml:/etc/codex/requirements.toml:ro" \
    -v "$PWD/tests/container-restore.mjs:/tmp/container-restore.mjs:ro" \
    "$image" node /tmp/container-restore.mjs "$phase"
done
# Exercise the real CLI and managed hooks across a second actual replacement.
for phase in prepare verify; do
  docker run --rm --user 1000:1000 --cap-drop ALL --pids-limit 512 \
    --memory 1280m --memory-swap 1280m --cpus 0.9 \
    --security-opt no-new-privileges:true \
    --security-opt "seccomp=$PWD/runtime/seccomp.json" --security-opt apparmor=codex-terminal \
    -v "$check_root/workspace:/workspace" -v "$check_root/config:/home/node/.codex" \
    -e CODEX_HOME=/home/node/.codex/real-restore-test \
    -v "$PWD/runtime/session-manager.mjs:/opt/codex-session.mjs:ro" \
    -v "$PWD/runtime/session-hooks.toml:/etc/codex/requirements.toml:ro" \
    -v "$PWD/tests/codex-startup.mjs:/tmp/codex-startup.mjs:ro" \
    -v "$PWD/tests/real-codex-restore.mjs:/tmp/real-codex-restore.mjs:ro" \
    "$image" node /tmp/real-codex-restore.mjs "$phase"
done
python3 tools/proxy-check.py
