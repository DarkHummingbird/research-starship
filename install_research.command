#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE_ROOT="$(cd "$PROJECT_ROOT/../.." && pwd)"
VENV_ROOT="${PERSONAL_AGENT_VENV:-$WORKSPACE_ROOT/../.personal-agent-venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "未找到 Python 3。请先安装 Python 3，再重新双击本安装程序。"
  read -r -p "按回车关闭" _
  exit 1
fi

if [[ ! -x "$VENV_ROOT/bin/python" ]]; then
  echo "正在创建独立运行环境…"
  "$PYTHON_BIN" -m venv "$VENV_ROOT"
fi

echo "正在安装科研星舰运行组件…"
"$VENV_ROOT/bin/python" -m pip install --disable-pip-version-check -r "$PROJECT_ROOT/requirements.txt" -c "$PROJECT_ROOT/requirements.lock"

echo
printf '安装完成。接下来配置模型服务商；配置只保存在本机。\n'
"$VENV_ROOT/bin/python" "$PROJECT_ROOT/scripts/configure_research.py"

export PERSONAL_AGENT_VENV="$VENV_ROOT"
exec "$PROJECT_ROOT/start_research.sh"
