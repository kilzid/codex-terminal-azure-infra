# Acceptance and rollout evidence

Run `bash tools/check.sh` on every PR. CI also runs `tools/container-check.sh` on Linux under the same memory, CPU, capability, seccomp and AppArmor settings as the VM. The test image always comes from the immutable lockfile.

Image updater tests cover registry digest verification, Linux AMD64 support, terminal-only lockfile changes, duplicate PR reuse, modified branch rejection, failed and stale checks, fresh-main and exact-head merge guards, explicit production dispatch and dispatch recovery. A live no-update discovery run verifies registry access; it does not prove the new-image PR/merge path. Observe a real bot image update through Validate, protected merge, Production apply and VM verification before claiming that full path has passed.

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

The administrator-only `operations/acceptance.py` performs the isolated fault and restore exercise after a clean-clone fresh deployment. Tag that temporary resource group with `purpose=codex-infra-acceptance` and use its separate private directory:

```sh
python3 operations/acceptance.py --private /absolute/private/isolated-test
```

It completes a backup, writes newer data, substitutes a deliberately incompatible terminal image through the host transaction, verifies rollback and unchanged auth/TLS containers, restores the backup's OS disk into a separate tagged group, attaches it only as a data disk and checks workspace/history markers read-only. It refuses the adopted profile and resource groups without the explicit test tag. Synthetic test contents and all backup/restore identifiers stay private.

Explicit cleanup is a separate administrator operation:

```sh
python3 operations/acceptance.py --cleanup --private /absolute/private/isolated-test
```

It removes test compute, disks and networking, while retaining the vault's enforced fourteen-day backup soft deletion. Its private cleanup record states when that retained vault can be reviewed for final removal. It never deletes the subscription's shared Azure Backup snapshot resource group. Normal reconciliation has no permission to perform this cleanup.

After compute cleanup succeeds, remove the temporary Entra applications, deployment role assignments/definitions, budget and monitoring:

```sh
python3 operations/acceptance.py --cleanup-identity --private /absolute/private/isolated-test
```

This verifies the isolated group tag and both exact application bindings before removing permissions. It keeps the vault and its retained recovery points; it does not change GitHub production secrets or account settings.
