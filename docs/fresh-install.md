# Fresh installation

Fork this public repository. Your fork is authoritative for your deployment. Keep an administrator recovery copy outside the clone; the production workflow will not need your administrator login or key.

1. Install Python 3.12+, Git, Azure CLI/Bicep, GitHub CLI and OpenSSL. Create a virtual environment and install `requirements.txt`. Sign into GitHub with repository administration access and Azure with the intended tenant/subscription administrator. Register the subscription providers during bootstrap. Creating Graph role grants requires tenant administrator privileges; Azure group, role and budget creation requires the corresponding subscription privileges.
2. Create a mode-0700 private directory outside the clone. Copy `config/private.example.json` to `private.json` there, mode 0600. Replace every placeholder. Obtain the intended administrator and allowed user's **object IDs**, not email addresses. Use your own fork's repository name. Choose a unique Azure resource group and prefix; set VM/vault/backup names consistently. Set the budget start date to the first day of the current month. Generate an Ed25519 administrator key there and put only its public key in the private JSON. Port 22 remains closed; retain the private key for recovery.
3. Review `config/deployment.json` and `images.lock.json`. The default is B1ms with seven-day daily backups. Optional retained storage, share backups and audit workspace are disabled for a fresh installation. Entra Security Defaults must already be enabled: bootstrap verifies the live policy and does not change tenant-wide security policy.
4. Run the complete administrator bootstrap:

   ```sh
   ./codex-infra bootstrap --fresh --private "$HOME/.local/share/codex-infra/production"
   ```

   It creates the owned runtime application, the deployment application and service principal, exact GitHub OIDC trust, restricted Azure permissions and the `production` environment restricted to `main`. It creates the VM and records its Azure hostname and static public IP privately, generates the initial runtime credential/cookie key, configures the host and uploads the private inputs as environment secrets. It configures no environment reviewer gate. The command never prints account identifiers, domains or rendered parameters.

5. Run `plan`, then `verify`. Sign in through the private recorded HTTPS URL as the assigned owner, complete MFA, create a file and confirm native terminal access. Save and reopen a conversation. An unassigned user and anonymous WebSocket/API request must fail. Create and restore an isolated backup as described in [recovery](recovery.md).
6. Make an encrypted offline recovery copy. Open a feature-branch pull request with any public setting changes; a merge into main activates automatic reconciliation. The repository's initial implementation pull request must be merged before the production workflow exists on main.

The Azure hostname works without an external domain. To use a custom hostname later, create the external DNS record, update the private `hostname`, rerun bootstrap to publish updated environment inputs, then plan/apply. Domain registration and DNS provider administration stay outside this repository.

A fresh installation must not point at an existing VM. Use the [adoption procedure](adoption.md) for an existing deployment. The CLI will not infer consent to replace it.

For an isolated acceptance deployment, use a separate private directory/resource group and `bootstrap --fresh --no-publish`. This explicitly keeps the existing GitHub environment secrets intact. It does not authorize deploying test settings to production.
