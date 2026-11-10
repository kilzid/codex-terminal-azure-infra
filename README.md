# Codex Terminal Azure Infrastructure

Deploy an Entra-protected [Codex terminal](https://github.com/dorseg/codex-terminal) into your own Azure account. This repository owns the infrastructure, host configuration, security policy and deployed image versions.

Fork this repository, then follow [fresh installation](docs/fresh-install.md) or [adoption](docs/adoption.md). The default baseline is Debian 12 on **Standard_B1ms**, a detached persistent OS disk, daily seven-day VM backups, HTTPS and access restricted to one immutable Entra user ID. SSH is closed. The terminal receives neither Azure identity nor proxy credentials.

The repository contains:

- `infra/`: networking, VM, backup, monitoring and optional retained recovery modules.
- `runtime/`: Compose rendering, Caddy, Entra proxy, host reconciliation and sandbox policies.
- `operations/`: the administrator and deployment CLI.
- `config/`: public deployment settings; `images.lock.json`: immutable container and OS versions.
- `schemas/`: separate validation for public settings, private inputs and runtime credentials.

Use Python 3.12+, Azure CLI with Bicep, GitHub CLI, OpenSSL and an administrator account for bootstrap. Routine GitHub deployment uses Azure OIDC with no stored Azure client password.

```sh
python3 -m venv /tmp/codex-infra-venv
. /tmp/codex-infra-venv/bin/activate
pip install -r requirements.txt
./codex-infra --help
./codex-infra plan --private "$HOME/.local/share/codex-infra/production"
```

Changes go through a feature branch and pull request. Merging into `main` automatically tests and deploys the latest desired revision, with no environment reviewer approval. Changes can immediately interrupt active terminal sessions. Workspace files, saved authentication and conversation history remain on disk. Terminal windows, splits, directories and tracked Codex conversations restore automatically; every automatic resume uses YOLO mode (`--dangerously-bypass-approvals-and-sandbox`) and waits for input. Running commands still stop. After first installation, reopen existing conversations once to enable tracking. See [restoration behavior](docs/operations.md#terminal-restoration).

[Operations and rollback](docs/operations.md), [recovery](docs/recovery.md), [security and privacy](SECURITY.md), and [acceptance testing](docs/acceptance.md) describe the operational contract. A daily check at **02:23 UTC** discovers a new terminal image, opens a digest-only PR, and merges it only after both required checks pass on that exact commit. It then starts the ordinary production deployment and VM verification. Other images and infrastructure remain manual changes.

Licensed under Apache-2.0. Moby attribution and its original license are retained in [runtime/NOTICE](runtime/NOTICE) and [runtime/MOBY-LICENSE](runtime/MOBY-LICENSE).
