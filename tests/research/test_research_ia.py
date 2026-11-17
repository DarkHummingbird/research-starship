from html.parser import HTMLParser
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]


class ResearchPageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.views = []
        self.nav_targets = []
        self.generation_options = []
        self.in_generation_select = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if element_id := attrs.get("id"):
            self.ids.append(element_id)
        if view := attrs.get("data-view"):
            self.views.append(view)
        if target := attrs.get("data-view-target"):
            self.nav_targets.append(target)
        if tag == "select" and element_id == "generationKind":
            self.in_generation_select = True
        elif tag == "option" and self.in_generation_select:
            self.generation_options.append(attrs.get("value"))

    def handle_endtag(self, tag):
        if tag == "select" and self.in_generation_select:
            self.in_generation_select = False


def test_research_page_has_home_and_four_task_workspaces():
    html = (ROOT / "static/research.html").read_text(encoding="utf-8")
    parser = ResearchPageParser()
    parser.feed(html)

    assert parser.views == ["home", "library", "review", "chat", "data"]
    assert parser.nav_targets == ["home", "library", "review", "chat", "data"]
    assert len(parser.ids) == len(set(parser.ids))
    assert "latex" not in parser.generation_options
    assert "恢复完整备份" not in html
    assert "restorePreviewForm" not in html
    assert "不提供整库恢复" in html
    assert "创建完整备份" not in html
    assert "exportLibrary" not in html
    assert "离开科研工作区，打开助手" in html
    assert "诊断与原始输出" in html
    for internal_term in ("P4", "Personal Agent", "本地 RAG"):
        assert internal_term not in html


def test_research_view_interactions():
    subprocess.run(
        ["node", "tests/unit/research_ia_ui.mjs"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def test_full_backup_creation_is_removed(client, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path / "research"))
    headers = auth("removed-full-backup")
    for path in ("/api/research/backup-tasks/full", "/api/research/export"):
        response = client.post(path, json={"confirmed": True}, headers=headers)
        assert response.status_code == 410
        assert response.json()["detail"]["code"] == "full_backup_removed"
