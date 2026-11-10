#!/bin/sh
# Run inside the unprivileged terminal container; no model login is required.
set -eu
test "$(id -u)" = 1000
test ! -S /var/run/docker.sock
codex --version

# Published Codex versions expose either an automatic platform helper or an
# explicit linux subcommand. Select a workspace policy from the bundled help;
# never fall back to full access or treat a read-only run as a successful write.
set -- sandbox
help=$(codex "$@" --help)
if printf '%s\n' "$help" | grep -Eq '^[[:space:]]+linux[[:space:]]'; then
  set -- "$@" linux
  help=$(codex "$@" --help)
fi
if printf '%s\n' "$help" | grep -q -- '--permission-profile'; then
  set -- "$@" --permission-profile :workspace --cd /workspace
elif printf '%s\n' "$help" | grep -q -- '--full-auto'; then
  set -- "$@" --full-auto
else
  printf '%s\n' "$help" >&2
  echo 'No supported explicit workspace sandbox policy; review this image version.' >&2
  exit 1
fi

cd /workspace
workspace_check=$(mktemp -d /workspace/.sandbox-check-XXXXXX)
outside_check=$(mktemp -d /home/node/.codex/.sandbox-check-XXXXXX)
trap 'rm -rf "$workspace_check" "$outside_check"' EXIT HUP INT TERM
printf outer-control > "$outside_check/control"
codex "$@" -- /bin/sh -ceu 'printf sandbox-ok > "$1/allowed"' sh "$workspace_check"
test "$(cat "$workspace_check/allowed")" = sandbox-ok
printf 'INNER_SANDBOX_WORKSPACE_WRITE_OK\n'
if codex "$@" -- /bin/sh -ceu 'printf must-deny > "$1/denied"' sh "$outside_check"; then
  echo 'Inner sandbox unexpectedly wrote outside the workspace.' >&2
  exit 1
fi
test ! -e "$outside_check/denied"
printf 'INNER_SANDBOX_WRITE_DENIED\n'
