#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
# Start ./run.sh serve first. This publishes the authenticated API only.
bridge_python=".venv/bin/python"
if [[ ! -x "$bridge_python" ]]; then bridge_python="python3"; fi
bridge_port="$($bridge_python -c 'from wxagentbot.config import load_settings; print(load_settings("config.yml").port)')"
exec instabox-port publish "$bridge_port"
