# Backup and recovery

Git recreates infrastructure and configuration. Private backups restore workspace files, Codex authentication and conversation history. Neither source alone is a complete recovery system.

Keep a separately encrypted recovery copy of `private.json`, `runtime-secrets.json`, resource inventory, adoption baseline, the active credential mapping and the VM administrator key. Keep its decryption key separately and test decryption. Also retain the old private deployment directory until adoption, a fresh installation, and an isolated restoration have been validated. GitHub environment secrets cannot be downloaded later. A machine-local mode-0700 directory is a working recovery copy, not an offline encrypted backup.

Before a disruptive deployment, the CLI starts an on-demand VM backup and waits for status `Completed`. Pending, failed or timed-out backups prevent mutation. The baseline policy runs daily, retains recovery points for seven days and instant snapshots for two days. Optional retained Azure Files shares have their own seven-day backup policy and fourteen-day share soft deletion; audit workspace retention is thirty days. Disabling an optional module does not delete its resources in incremental mode; use a separately reviewed cleanup operation for permanent removal.

To restore:

1. Freeze automatic deployment while investigating by disabling the production workflow in GitHub. Record the intended Git commit and recovery point privately.
2. Clone the repository and decrypt the private recovery bundle outside the checkout. Authenticate as the expected administrator; verify tenant and subscription.
3. For total VM loss, restore the protected VM/disks into an isolated recovery resource group through Azure Backup's restore workflow. Preserve the original recovery points. Alternatively bootstrap fresh infrastructure and mount a restored data disk read-only for extraction.
4. Restore `/srv/codex-terminal/workspace` and `/srv/codex-terminal/codex-config`, preserving ownership (1000:1000), file modes, SQLite databases and their WAL files consistently. Restore Caddy data if preserving certificate state. Do not overwrite newer production workspace data during a runtime image rollback.
5. Restore or explicitly rotate runtime client/cookie credentials from the private bundle. Reconcile infrastructure, HTTPS hostname and Entra callbacks using the chosen Git revision. Keep port 22 closed and the VM free of managed identity.
6. Run `verify`, test owner sign-in and native terminal access, and compare restored checksums from the isolated test. Confirm that saved conversations are visible; reopening is manual.
7. Resume the workflow after the repository describes the recovered desired state. Securely retain or explicitly clean up the isolated recovery deployment according to the retention policy.

Test restoration in a separate resource group with synthetic markers and a consistent stopped-data copy; never run a restore experiment over production paths. Record only sanitized pass/fail results in public release notes. Disk, share, workspace, vault and backup deletion require an explicit administrator cleanup operation with exact private resource scope; they are outside automatic deployment.
