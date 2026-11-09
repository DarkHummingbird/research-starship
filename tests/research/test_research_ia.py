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


def test_research_page_has_four_task_workspaces():
    html = (ROOT / "static/research.html").read_text(encoding="utf-8")
    parser = ResearchPageParser()
    parser.feed(html)

    assert parser.views == ["library", "review", "chat", "data"]
    assert parser.nav_targets == ["library", "review", "chat", "data"]
    assert len(parser.ids) == len(set(parser.ids))
    assert "latex" not in parser.generation_options
    assert "恢复完整备份" in html
    assert "只支持恢复到没有科研数据的账号" in html
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
