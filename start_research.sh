#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${PERSONAL_AGENT_VENV:-/Users/ddl/Documents/Codex/科研星舰/.personal-agent-venv}/bin/python"
export DATA_DIR="${DATA_DIR:-/Users/ddl/Documents/Codex/科研星舰/out/科研星舰/运行数据/普通助手}"
export RESEARCH_DATA_DIR="${RESEARCH_DATA_DIR:-/Users/ddl/Documents/Codex/科研星舰/out/科研星舰/运行数据/科研}"
export PROACTIVE_ENABLED=0
export FEISHU_WS_ENABLED=0
export LANGFUSE_ENABLED=0
export HOST=127.0.0.1
export PORT="${PORT:-8787}"
PROVIDER_CONFIG="${RESEARCH_PROVIDER_CONFIG:-$(cd "$PROJECT_ROOT/../.." && pwd)/运行数据/配置/providers.env}"
if [[ -f "$PROVIDER_CONFIG" ]]; then
  set -a
  source "$PROVIDER_CONFIG"
  set +a
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "没有找到 Python 运行环境。请先按交付记录安装依赖，或设置 PERSONAL_AGENT_VENV。" >&2
  exit 1
fi

cd "$PROJECT_ROOT"
"$PYTHON" -c 'from app.research.local_components import component_status; status=component_status(); missing=[name for name,item in status.items() if name != "rerank" and not item["available"]]; print("本地隐私组件：", "、".join(missing) if missing else "生成、向量化和 OCR 均可用"); print("非 L3 资料可使用已配置的 API；仅 L3 资料强制要求本地组件")'
mkdir -p "$DATA_DIR" "$RESEARCH_DATA_DIR"
if [[ -z "${AUTH_SECRET:-}" ]]; then
  SECRET_FILE="$RESEARCH_DATA_DIR/.auth-secret"
  if [[ ! -f "$SECRET_FILE" ]]; then
    umask 077
    "$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(32))' > "$SECRET_FILE"
    chmod 600 "$SECRET_FILE"
  fi
  export AUTH_SECRET="$(<"$SECRET_FILE")"
fi

echo "科研星舰将打开 http://$HOST:$PORT/research"
echo "Research data: ${RESEARCH_DATA_DIR}. Runtime logs contain local access metadata only."
(
  for _ in {1..30}; do
    if "$PYTHON" -c 'import os,urllib.request; urllib.request.urlopen("http://"+os.environ["HOST"]+":"+os.environ["PORT"]+"/research",timeout=1)' 2>/dev/null; then
      if [[ "${RESEARCH_OPEN_BROWSER:-1}" != "0" ]]; then
        open "http://$HOST:$PORT/research"
      fi
      break
    fi
    sleep 1
  done
) &
"$PYTHON" -m app.main
