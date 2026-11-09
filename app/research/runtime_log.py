from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.research import store


RETENTION_DAYS = 30
_FILENAME = re.compile(r"runtime-(\d{4}-\d{2}-\d{2})\.jsonl$")
_LOCK = threading.Lock()


def _directory() -> Path:
    directory = store.data_dir() / "logs" / "runtime"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    return directory


def cleanup_runtime_logs(*, now: datetime | None = None) -> int:
    current = now or datetime.now(timezone.utc)
    cutoff = current.date() - timedelta(days=RETENTION_DAYS)
    removed = 0
    directory = _directory()
    for path in directory.glob("runtime-*.jsonl"):
        match = _FILENAME.fullmatch(path.name)
        if not match:
            continue
        try:
            date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if date < cutoff:
            path.unlink()
            removed += 1
    return removed


def log_request(*, method: str, path: str, status_code: int, elapsed_seconds: float) -> None:
    now = datetime.now(timezone.utc)
    event = {
        "time": now.isoformat(),
        "method": method,
        "path": path,
        "status_code": status_code,
        "elapsed_ms": round(elapsed_seconds * 1000, 3),
    }
    with _LOCK:
        removed = cleanup_runtime_logs(now=now)
        if removed:
            event["expired_files_removed"] = removed
        target = _directory() / f"runtime-{now.date().isoformat()}.jsonl"
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        target.chmod(0o600)
