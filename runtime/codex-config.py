"""Set VM-compatible Codex paths and defaults, preserving other user settings."""
import os
from pathlib import Path
import re


def configured(text):
    section = re.search(r'^\s*\[', text, re.M)
    root = text[:section.start()] if section else text
    sections = text[section.start():] if section else ''
    root = re.sub(r'^\s*(sqlite_home|sandbox_mode|approval_policy|cli_auth_credentials_store)\s*=.*(?:\n|$)', '', root, flags=re.M)
    return ('sqlite_home = "/home/node/.codex/sqlite"\n'
            'sandbox_mode = "workspace-write"\napproval_policy = "on-request"\n'
            'cli_auth_credentials_store = "file"\n'
            + root.rstrip() + '\n' + sections)


if __name__ == '__main__':
    root = Path('/srv/codex-terminal/codex-config')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = root / 'config.toml'
    config.write_text(configured(config.read_text() if config.exists() else ''))
    config.chmod(0o600)
    os.chown(config, 1000, 1000)
    sqlite = root / 'sqlite'
    sqlite.mkdir(mode=0o700, exist_ok=True)
    os.chown(sqlite, 1000, 1000)
