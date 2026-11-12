from __future__ import annotations

import argparse
import getpass
import os
import shlex
from pathlib import Path


PROVIDERS = {
    "1": ("Kimi", "KIMI_API_KEY", "https://api.moonshot.cn/v1", "kimi-k2.6"),
    "2": ("DeepSeek", "DEEPSEEK_API_KEY", "https://api.deepseek.com/v1", "deepseek-chat"),
    "3": ("Qwen", "DASHSCOPE_API_KEY", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "4": ("OpenAI", "OPENAI_API_KEY", "https://api.openai.com/v1", "gpt-4o-mini"),
}


def default_config_path() -> Path:
    root = Path(__file__).resolve().parents[1]
    return root.parent.parent / "运行数据" / "配置" / "providers.env"


def write_provider_config(path: Path, choice: str, model: str, api_key: str) -> None:
    if choice not in PROVIDERS:
        raise ValueError("未知服务商")
    name, key_name, base_url, default_model = PROVIDERS[choice]
    model = model.strip() or default_model
    if not api_key.strip():
        raise ValueError("未填写 API Key，配置未保存")
    lines = [
        "# 由科研星舰首次配置向导生成；不要提交到源码仓库。\n",
        f"{key_name}={shlex.quote(api_key.strip())}\n",
        f"LLM_FAST_MODEL={shlex.quote(model)}\n",
        f"LLM_STRONG_MODEL={shlex.quote(model)}\n",
        f"LLM_FAST_BASE_URL={shlex.quote(base_url)}\n",
        f"LLM_STRONG_BASE_URL={shlex.quote(base_url)}\n",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.writelines(lines)
    path.chmod(0o600)
    print(f"{name} 已配置完成。配置保存在本机：{path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="安装完成后的科研星舰服务商配置")
    parser.add_argument("--config", type=Path, default=default_config_path())
    args = parser.parse_args()
    print("请选择服务商：")
    for choice, (name, *_rest) in PROVIDERS.items():
        print(f"  {choice}. {name}")
    choice = input("服务商序号 [1]：").strip() or "1"
    if choice not in PROVIDERS:
        raise SystemExit("服务商选择无效，未保存配置")
    default_model = PROVIDERS[choice][3]
    model = input(f"模型名称 [{default_model}]：").strip() or default_model
    api_key = getpass.getpass("API Key（输入时不显示）：")
    try:
        write_provider_config(args.config, choice, model, api_key)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
