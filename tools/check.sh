#!/bin/bash
set -euo pipefail
python3 tools/privacy.py
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 operations/cli.py --help >/dev/null
bash -n tools/check.sh tools/container-check.sh runtime/boot-guards.sh
sh -n codex-infra runtime/network-rules.sh runtime/sandbox-check.sh
# The template contains no private parameter values. No generated artifact is uploaded.
az bicep build --file infra/main.bicep --outfile "${RUNNER_TEMP:-/tmp}/codex-infra-template.json" >/dev/null
