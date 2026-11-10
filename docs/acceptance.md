# Acceptance and rollout evidence

Run `bash tools/check.sh` on every PR. CI also runs `tools/container-check.sh` on Linux under the same memory, CPU, capability, seccomp and AppArmor settings as the VM. The test image always comes from the immutable lockfile.

The local unit suite covers tenant/subscription/principal rejection for administrator and workload authentication, repository/branch/event restrictions, owner authorization and token isolation, schema and immutable-image validation, unchanged configuration, terminal-only image changes, failed-image rollback without changing workspace data, concurrent-change rejection and privacy scanning.

Before initial rollout, record the following evidence privately and publish only sanitized status:

| Check | Required result |
| --- | --- |
| Exact private-value and full reachable-history scan | No account inputs or secrets |
| PR/fork workflow and environment review | No production secrets or Azure identity |
| Live OIDC exchange with wrong repo/environment/ref | Rejected by exact federation subject |
| Repeat unchanged apply | Same container IDs, credential and file hashes |
| Terminal digest update | Auth/TLS IDs unchanged; files, auth and history retained |
| Native PTY, WebSocket, origin and sandbox checks | Successful protected access and rejected bypasses |
| Deliberately failing runtime image in isolated deployment | Previous image/config restored; newest workspace data retained |
| Clean clone into temporary resource group | Fresh bootstrap, runtime and security verification pass |
| Isolated backup restoration | Synthetic workspace/history checksums match |
| Existing production adoption | Same VM instance/disk/IP and service IDs; uninterrupted access |

Do not describe a skipped cloud or interactive test as passed. Automated public health checks verify protection and redirects; owner MFA sign-in and reopening a saved conversation require an interactive user check. Keep raw cloud state, backup IDs, host hashes, browser cookies and logs out of public PRs, workflow artifacts and summaries.

The initial PR activates deployment only when merged. Do not merge until its published validation and privately retained rollout evidence support the intended production adoption. Permanent cleanup is outside the automatic deployment path.
