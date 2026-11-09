#!/usr/bin/env bash
# Run from the machine's normal desktop/Terminal session.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

if [[ ! -x .venv/bin/python ]] || ! .venv/bin/python -c 'import openai_codex, fastapi, uvicorn, PIL, gi, yaml' >/dev/null 2>&1; then
  ./install.sh
fi

./run.sh check
./run.sh doctor

if ! .venv/bin/python - <<'PY'
from wxagentbot.config import load_settings
settings = load_settings('config.yml')
routes = [route.name for route in settings.conversations if route.enabled]
if not routes:
    print('尚未启用监听会话：请在 config.yml 中将目标会话的 enabled 改为 true。')
    raise SystemExit(1)
print('监听会话：' + '、'.join(routes))
PY
then
  exit 1
fi

printf '%s\n' '正在启动 wxagentbot；保持此终端打开。按 Ctrl+C 停止。'
exec ./run.sh serve
