#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
bridge_python=".venv/bin/python"
if [[ ! -x "$bridge_python" ]]; then bridge_python="python3"; fi
exec "$bridge_python" -m wxagentbot "$@"
