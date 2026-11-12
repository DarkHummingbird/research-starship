import importlib.util
import shlex
import stat
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_configurator():
    path = ROOT / "scripts/configure_research.py"
    spec = importlib.util.spec_from_file_location("research_configurator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_provider_configuration_is_written_outside_source_with_private_permissions(tmp_path):
    module = load_configurator()
    target = tmp_path / "config" / "providers.env"
    module.write_provider_config(target, "3", "qwen-plus", "synthetic-key-足够安全")

    content = target.read_text(encoding="utf-8")
    assert "DASHSCOPE_API_KEY=" in content
    assert "LLM_FAST_MODEL=qwen-plus" in content
    assert "LLM_STRONG_MODEL=qwen-plus" in content
    assert "compatible-mode" in content
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    values = dict(line.split("=", 1) for line in content.splitlines() if line and not line.startswith("#"))
    assert shlex.split(values["DASHSCOPE_API_KEY"]) == ["synthetic-key-足够安全"]
    assert ROOT not in target.parents


def test_provider_configuration_rejects_empty_key(tmp_path):
    module = load_configurator()
    target = tmp_path / "providers.env"
    try:
        module.write_provider_config(target, "1", "kimi-k2.6", "")
    except ValueError as exc:
        assert "未填写" in str(exc)
    else:
        raise AssertionError("empty API key must not create a configuration")
    assert not target.exists()
