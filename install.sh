#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if command -v uv >/dev/null 2>&1; then
  if [[ ! -x .venv/bin/python ]]; then uv venv --python /usr/bin/python3 --system-site-packages .venv; fi
  uv pip install --python .venv/bin/python -e '.[bridge]'
else
  if [[ ! -x .venv/bin/python ]]; then python3 -m venv --system-site-packages .venv; fi
  .venv/bin/python -m pip install -e '.[bridge]'
fi
if [[ ! -f config.yml ]]; then
  cp config.example.yml config.yml
fi
./run.sh init
./run.sh check
./run.sh doctor
