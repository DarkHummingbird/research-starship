from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.research.runtime_log import cleanup_runtime_logs


def test_research_access_log_is_local_metadata_only_and_expires_after_30_days(client, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    headers = auth("p4-runtime-log-user")
    old = client.post("/api/research/projects", json={"name": "日志验收"}, headers=headers)
    assert old.status_code == 201
    response = client.get("/api/research/capabilities?query=must-not-appear", headers=headers)
    assert response.status_code == 200
    directory = tmp_path / "research" / "logs" / "runtime"
    files = list(directory.glob("runtime-*.jsonl"))
    assert len(files) == 1
    assert files[0].stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["method"] == "POST"
    assert first["status_code"] == 201
    assert second["method"] == "GET"
    assert second["status_code"] == 200
    assert "elapsed_ms" in second
    assert "must-not-appear" not in files[0].read_text(encoding="utf-8")
    assert "p4-runtime-log-user" not in files[0].read_text(encoding="utf-8")
    assert "Authorization" not in files[0].read_text(encoding="utf-8")

    old_file = directory / "runtime-2000-01-01.jsonl"
    old_file.write_text("synthetic old log\n", encoding="utf-8")
    removed = cleanup_runtime_logs(now=datetime.now(timezone.utc))
    assert removed == 1
    assert not old_file.exists()
    boundary = (datetime.now(timezone.utc).date() - timedelta(days=30)).isoformat()
    boundary_file = directory / f"runtime-{boundary}.jsonl"
    boundary_file.write_text("synthetic boundary log\n", encoding="utf-8")
    assert cleanup_runtime_logs() == 0
    assert boundary_file.exists()
