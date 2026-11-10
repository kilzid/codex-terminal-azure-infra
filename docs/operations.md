# Operations

All commands use `./codex-infra COMMAND --private /absolute/private/directory`. That directory must be outside the clone. Run `plan` before applying locally; the plan and raw ARM preview remain private. The CLI exposes `bootstrap`, `adopt`, `plan`, `apply`, `verify`, `rollback`, and `rotate-credentials`. `bootstrap --fresh` creates a fresh VM after privileged setup. Bootstrap and adoption are administrator operations; routine deployment uses the workload principal.

`apply` reads actual infrastructure and managed host files, calculates differences, completes a VM backup before disruptive changes, retains previous runtime configuration and applies only changed components. No-op applications do not rotate credentials, recreate containers or rewrite equivalent configuration. The applied Git commit, lockfile and private configuration fingerprint are stored root-only on the VM. Scheduling runs verification, never automatic adoption of drift.

The production workflow uses a non-cancelling concurrency group. When it obtains the lock it checks out current `main` and runs validation and container tests against that actual revision. A queued old workflow therefore reconciles the latest desired revision. No PR, fork PR or feature branch can enter the production environment or obtain its Azure identity.

## Image updates

Edit only the relevant digest in `images.lock.json` on a feature branch, validate, and open a pull request. Tags and `latest` are rejected. The three images and the OS image version are separately pinned. Pull images before mutation. An update confined to the terminal digest recreates only the terminal container. The authentication and HTTPS container IDs must remain unchanged. Verification exercises native PTY/WebSocket access, same-origin checks, sandbox write allow/deny, persisted mounts and saved Codex authentication.

OS image version changes require an explicit replacement/recovery procedure; an automatic deployment refuses immutable VM changes. Updating the lockfile does not silently reimage an existing VM.

## Domains and resizing

Manage domain registration and DNS with your provider. Point an A record for the chosen hostname to the deployment's private recorded public IP (or a supported CNAME to its Azure hostname). Update `hostname` only in the private configuration and republish the environment secret using bootstrap. `plan` validates the change; `apply` adds the new Entra callback, updates only the affected authentication/TLS configuration, verifies public TLS and redirects, and removes the old callback. Failure restores the previous runtime configuration and callbacks. Never put your domain or DNS provider credentials in Git.

VM size and container limits are public settings in the selected profile. Keep B1ms during adoption. A later size change needs its own reviewed maintenance change. Apply completes a backup, deallocates the VM, updates its size and starts it again; a failed resize restores the previous size and starts it. Resizing interrupts active sessions. Automatic reconciliation must not replace the VM or change disk/IP identity to satisfy a size request.

## Credentials

Ordinary apply and adoption preserve both client and cookie secrets. `rotate-credentials` is an explicit administrator operation: create a replacement client credential, back up, update only authentication, verify access, upload the replacement to the environment and only then revoke the mapped old credential. The cookie key is preserved. On failure the old credential remains valid and a root-only/local pending recovery record identifies the replacement. Entra interactive sign-in should be checked after rotation; unattended health checks cannot prove a human's complete MFA browser experience. Never guess which historic credential is active or delete unmapped credentials.

Update your encrypted recovery copy whenever private settings or credentials change. Credentials expire; schedule rotation before their recorded expiry. Do not add an automatic rotate step to a no-op deployment.

## Rollback and interrupted deployment

Runtime verification failure restores the previous configuration and images. It never restores an old workspace snapshot. `rollback` explicitly selects the last recorded runtime revision (or `--revision` from private state), after completing a backup. Explicit rollback preserves the currently active client/cookie credentials so a previous rotation's revoked credential is not reinstated. Revert the corresponding repository commit through a pull request so the desired configuration agrees with the rollback. A pending host transaction stops the next apply and requires recovery; never erase it to force an update.

Infrastructure reconciliation is incremental and refuses deletion and immutable VM changes. Infrastructure failures are not automatically reversed through destructive ARM deployment. Resolve the private preview and recover explicitly. No automatic path permanently deletes data, workspaces or backups.

The host retains the configured number of private runtime revisions. VM recovery points use the configured daily retention; service-managed snapshot and backup artifacts follow Azure Backup policy. Only managed Run Command records older than the configured retention are removed automatically; records outside the managed prefix remain for the administrator to review. Never use Docker image pruning as part of deployment: offline rollback needs the previous image.
