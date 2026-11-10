# Security and privacy

Production inputs live in the GitHub `production` environment, outside Git history:

| Secret | Contents |
| --- | --- |
| `PRIVATE_CONFIG` | Validated account, identity, domain and resource mappings |
| `RUNTIME_SECRETS` | Existing runtime client credential, cookie key and credential metadata |
| `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` | OIDC login inputs |

Never place private inputs anywhere in the checkout, including ignored directories. CLI input directories must be outside it, mode 0700; files are mode 0600. Retain an encrypted offline recovery copy and the administrator SSH key separately. GitHub secrets cannot be read back through the API. A public fork must bootstrap its own environment and Azure identities.

Bootstrap configures an exact federation subject containing immutable repository ID, owner ID, `production` and `refs/heads/main`. The environment permits only the main branch. PR validation has read-only GitHub permissions, no environment, no production secrets and no OIDC permission. No workflow uses `pull_request_target`, untrusted artifacts, or raw service-log publication. Both local user authentication and workload identity verify tenant, subscription and immutable principal ID before cloud operations.

The deployment principal has `Application.ReadWrite.OwnedBy` on Graph and owns only the runtime application and service principal. `Policy.Read.All` verifies live Security Defaults; a missing permission or disabled policy stops deployment. Bootstrap alone grants Graph permissions, creates Azure role definitions and assignments, and establishes runtime owners and user assignment. It never enables or changes tenant-wide policy automatically. Azure roles are scoped to the application resource group and the existing budget resource. Routine roles do not permit deleting disks, workspaces, storage accounts, shares, vaults, protected items or recovery points, or creating role assignments.

A local administrator may use a short-lived certificate on the deployment application to read the live MFA policy when Azure CLI's delegated Graph token lacks that scope. Preflight still verifies the signed-in administrator. The certificate credential expires after fifteen minutes, is removed in `finally`, and its private key is destroyed with its temporary directory. No Azure client password is created. An interrupted process may leave an expiring public certificate; inspect application credentials during recovery.

The VM has no managed identity and no public SSH. Only ports 80 (ACME/redirect) and 443 are allowed. The non-root terminal drops capabilities, uses a default-deny seccomp profile and AppArmor, and mounts only workspace and Codex data. Metadata endpoints are blocked before Docker startup. Authentication and TLS use separate containers; token and cookie secrets are absent from the terminal environment. The proxy validates tenant, issuer, audience, signature, nonce and PKCE, authorizes the immutable owner ID, and does not forward identity tokens to the shell. Client credentials and secure cookies are preserved across ordinary deployment and adoption.

CLI output is structured and contains only fixed status codes, counts and affected service names. External command stdout/stderr, API bodies, rendered proxy configuration, resource IDs, domains, IPs and private parameter files are captured privately and never printed. CI masks scalar secret values before Azure login, uses ephemeral parameter files and uploads no artifacts or service logs. Do not enable Actions step debugging, shell tracing, Azure `--debug`, `docker compose config` output, or arbitrary service-log collection in public workflows. Azure Run Command uses protected parameters and a bounded output protocol; the encrypted adoption export emits only ciphertext.

`tools/privacy.py` checks the explicit `RELEASE_FILES.json` whitelist, working files, all reachable Git blobs, commit messages and author metadata. It rejects common credential patterns, account UUIDs, personal emails and public addresses. Locally run it with `--private` to compare against exact account values. CI has no production values during PR validation, so neither pattern checks nor exact matching should be treated as a complete generic secret detector; review additions to the whitelist and never import old history or copy deployment directories wholesale.

Report vulnerabilities privately through the repository owner's security contact mechanisms. Do not post credentials, account identifiers, domains, addresses, rendered configuration, operational state or raw logs in public issues.

OIDC follows [GitHub's Azure guidance](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-azure) and [custom subject documentation](https://docs.github.com/en/actions/reference/security/oidc). Graph permissions follow [Microsoft's permissions reference](https://learn.microsoft.com/en-us/graph/permissions-reference).
