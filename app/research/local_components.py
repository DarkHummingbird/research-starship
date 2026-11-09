from __future__ import annotations

import json
import os
import platform
import subprocess
import threading
from pathlib import Path
from typing import Any


P0_ROOT = Path(__file__).resolve().parents[3]
EMBEDDING_PYTHON = P0_ROOT / ".embedding-venv/bin/python"
EMBEDDING_MODEL = P0_ROOT / "本地检索模型"
OCR_PYTHON = P0_ROOT / ".ocr-venv/bin/python"
OCR_MODELS = P0_ROOT / "OCR缓存/official_models"
LLAMA_COMPLETION = P0_ROOT / "离线模型/llama-b12138/llama-completion"
LLM_MODEL = P0_ROOT / "离线模型/qwen3.5-9b-Q4_K_M.gguf"
_GENERATION_LOCK = threading.Lock()


class LocalComponentError(RuntimeError):
    def __init__(self, component: str, message: str):
        super().__init__(message)
        self.component = component
        self.message = message


def _path(env_name: str, default: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser((os.getenv(env_name) or str(default)).strip())))


def embedding_python() -> Path:
    return _path("RESEARCH_EMBEDDING_PYTHON", EMBEDDING_PYTHON)


def embedding_model() -> Path:
    return _path("RESEARCH_EMBEDDING_MODEL_DIR", EMBEDDING_MODEL)


def ocr_python() -> Path:
    return _path("RESEARCH_OCR_PYTHON", OCR_PYTHON)


def ocr_models() -> Path:
    return _path("RESEARCH_OCR_MODEL_DIR", OCR_MODELS)


def llama_completion() -> Path:
    return _path("RESEARCH_LOCAL_LLM_COMPLETION", LLAMA_COMPLETION)


def llm_model() -> Path:
    return _path("RESEARCH_LOCAL_LLM_MODEL", LLM_MODEL)


def component_status() -> dict[str, Any]:
    embedding_ready = embedding_python().is_file() and (embedding_model() / "model.onnx").is_file() and (embedding_model() / "tokenizer.json").is_file()
    ocr_ready = ocr_python().is_file() and (ocr_models() / "PP-OCRv5_mobile_det").is_dir() and (ocr_models() / "PP-OCRv5_mobile_rec").is_dir()
    llm_ready = llama_completion().is_file() and llm_model().is_file()
    return {
        "embedding": {"available": embedding_ready, "model": "BAAI/bge-m3:onnx", "mode": "offline_subprocess"},
        "ocr": {"available": ocr_ready, "model": "PP-OCRv5_mobile", "mode": "offline_subprocess"},
        "generative_llm": {"available": llm_ready, "model": llm_model().name, "mode": "offline_subprocess", "max_concurrent_generations": 1},
        "rerank": {"available": False, "model": None, "mode": "not_integrated"},
    }


def _offline_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in list(env):
        if key.endswith("_API_KEY") or "TOKEN" in key:
            env.pop(key, None)
    env.update({
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
    })
    return env


def _offline_command(command: list[str]) -> list[str]:
    if platform.system() == "Darwin":
        return ["sandbox-exec", "-p", "(version 1)(allow default)(deny network-outbound)", *command]
    return command


def run_worker(python: Path, script: Path, payload: dict[str, Any], *, timeout: int, component: str) -> dict[str, Any]:
    if not python.is_file():
        raise LocalComponentError(component, "本地组件不存在")
    try:
        result = subprocess.run(
            _offline_command([str(python), str(script)]),
            input=json.dumps(payload, ensure_ascii=False), text=True,
            capture_output=True, timeout=timeout, env=_offline_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise LocalComponentError(component, "本地组件运行超时，未自动重试") from exc
    except OSError as exc:
        raise LocalComponentError(component, f"本地组件无法启动：{type(exc).__name__}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout)[-1000:]
        raise LocalComponentError(component, f"本地组件失败：{detail}")
    try:
        output = json.loads(result.stdout)
    except ValueError as exc:
        raise LocalComponentError(component, "本地组件返回了无效 JSON") from exc
    if output.get("error"):
        raise LocalComponentError(component, str(output["error"])[:1000])
    return output


def embed_texts(texts: list[str], *, timeout: int = 120) -> dict[str, Any]:
    status = component_status()["embedding"]
    if not status["available"]:
        raise LocalComponentError("embedding", "本地 BGE-M3 或运行环境不可用")
    worker = Path(__file__).with_name("embedding_worker.py")
    output = run_worker(
        embedding_python(), worker,
        {"model_dir": str(embedding_model()), "texts": texts},
        timeout=timeout, component="embedding",
    )
    vectors = output.get("vectors")
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise LocalComponentError("embedding", "本地向量数量与输入不一致")
    if any(not isinstance(vector, list) or len(vector) != 1024 for vector in vectors):
        raise LocalComponentError("embedding", "本地向量维度必须为 1024")
    return output


def generate(prompt: str, *, max_tokens: int = 768, timeout: int = 180) -> dict[str, Any]:
    status = component_status()["generative_llm"]
    if not status["available"]:
        raise LocalComponentError("generative_llm", "本地生成模型或 llama.cpp 不可用")
    command = [
        str(llama_completion()), "-m", str(llm_model()), "-c", "8192", "-ngl", "99",
        "-n", str(max_tokens), "--temp", "0", "--seed", "42", "--no-conversation",
        "--no-display-prompt", "-p",
        "<|im_start|>user\n" + prompt + "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n",
    ]
    try:
        with _GENERATION_LOCK:
            result = subprocess.run(
                _offline_command(command), capture_output=True, text=True,
                timeout=timeout, env=_offline_env(),
            )
    except subprocess.TimeoutExpired as exc:
        raise LocalComponentError("generative_llm", "本地生成超时，未自动重试或切换外部模型") from exc
    except OSError as exc:
        raise LocalComponentError("generative_llm", f"本地生成无法启动：{type(exc).__name__}") from exc
    if result.returncode != 0:
        raise LocalComponentError("generative_llm", "本地生成失败：" + (result.stderr or result.stdout)[-1000:])
    text = result.stdout.strip()
    if not text:
        raise LocalComponentError("generative_llm", "本地生成返回空内容")
    return {"text": text, "model": llm_model().name, "network": "denied_by_sandbox"}


def ocr_image(image_path: Path, *, timeout: int = 180) -> dict[str, Any]:
    status = component_status()["ocr"]
    if not status["available"]:
        raise LocalComponentError("ocr", "本地 OCR 或运行环境不可用")
    worker = Path(__file__).with_name("ocr_worker.py")
    return run_worker(
        ocr_python(), worker,
        {"model_dir": str(ocr_models()), "image": str(image_path)},
        timeout=timeout, component="ocr",
    )
