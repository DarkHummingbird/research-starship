from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from docx import Document
from pypdf import PdfReader

from app.research.chunking import chunk_text, parent_metadata
from app.research.parser import parse_markdown, validate_document_card


SUPPORTED_SUFFIXES = {".md", ".markdown", ".pdf", ".docx", ".tex", ".csv", ".html", ".htm"}


@dataclass
class ParsedInput:
    source_text: str
    card: dict[str, Any]
    chunks: list[dict[str, Any]]
    document_type: str
    processing_status: str = "ready"
    warnings: list[str] = field(default_factory=list)


def _card(
    *,
    document_id: str,
    version: int,
    title: str,
    confidentiality: str,
    sections: list[tuple[str, str, dict[str, Any]]],
    document_type: str,
    chunking_strategy: str = "auto",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    parent_groups: dict[str, list[str]] = {}
    parent_chapters: dict[str, str] = {}
    for chapter, content, locator in sections:
        key = str(locator.get("chapter") or locator.get("page") or locator.get("section") or locator.get("table") or chapter)
        parent_groups.setdefault(key, []).append(content.strip())
        parent_chapters[key] = str(locator.get("chapter") or chapter)
    chunks: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    for ordinal, (chapter, content, locator) in enumerate(sections, 1):
        content = content.strip()
        chunk_id = f"{document_id}:v{version}:c{ordinal}"
        parent_key = str(locator.get("chapter") or locator.get("page") or locator.get("section") or locator.get("table") or chapter)
        chunk = {
            "id": chunk_id, "document_id": document_id, "document_version": version,
            "ordinal": ordinal, "chapter": chapter, "content": content,
            "confidentiality": confidentiality,
            "locator_json": parent_metadata(
                parent_id=f"{document_id}:v{version}:p{parent_key}",
                parent_chapter=parent_chapters[parent_key],
                parent_content="\n\n".join(parent_groups[parent_key]),
                strategy=chunking_strategy, locator=locator,
            ),
        }
        chunks.append(chunk)
        entries.append({
            "entry_id": f"entry-{ordinal}", "statement": content,
            "confidentiality": confidentiality, "review_status": "pending",
            "suspicious": ["原文含待补或待核验标记"] if re.search(r"TODO|待补|待核验|无法识别", content, re.I) else [],
            "evidence": {
                "document_id": document_id, "version": version, "chunk_id": chunk_id,
                "chapter": chapter, "quote": content,
            },
        })
    card = {
        "schema_version": "research-document/v1", "title": title, "record_type": "source_statement",
        "research_fields": {"method": None, "baseline": None, "datasets": [], "metrics": [], "limitations": []},
        "entries": entries,
    }
    validate_document_card(card, chunks)
    return card, chunks


def _split_text(text: str, strategy: str = "auto") -> list[str]:
    return chunk_text(text, strategy)


def _pdf(content: bytes, document_id: str, title: str, confidentiality: str, chunking_strategy: str = "auto") -> ParsedInput:
    reader = PdfReader(io.BytesIO(content))
    if reader.is_encrypted:
        try:
            if reader.decrypt("") == 0:
                raise ValueError("加密 PDF 需要密码，当前不支持")
        except Exception as exc:
            raise ValueError("加密 PDF 需要密码，当前不支持") from exc
    sections: list[tuple[str, str, dict[str, Any]]] = []
    source_parts: list[str] = []
    empty_pages: list[int] = []
    for page_number, page in enumerate(reader.pages, 1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            text = ""
        if not text:
            empty_pages.append(page_number)
            continue
        for index, part in enumerate(_split_text(text, chunking_strategy), 1):
            chapter = f"第 {page_number} 页" + (f"（片段 {index}）" if index > 1 else "")
            sections.append((chapter, part, {"page": page_number, "part": index, "extraction": "pdf_text"}))
            source_parts.append(f"[{chapter}]\n{part}")
    warnings = [f"第 {','.join(map(str, empty_pages))} 页没有可提取文本，需手动执行 DeepSeek 在线 OCR 或本地 OCR"] if empty_pages else []
    if not sections:
        placeholder = "扫描页尚未执行 OCR，当前没有可核验正文。"
        sections = [("待 OCR", placeholder, {"extraction": "pending_ocr"})]
        source_parts = [placeholder]
    status = "needs_ocr" if empty_pages else "ready"
    card, chunks = _card(
        document_id=document_id, version=1, title=title, confidentiality=confidentiality,
        sections=sections, document_type="paper", chunking_strategy=chunking_strategy,
    )
    return ParsedInput("\n\n".join(source_parts), card, chunks, "paper", status, warnings)


def _docx(content: bytes, document_id: str, title: str, confidentiality: str, chunking_strategy: str = "auto") -> ParsedInput:
    document = Document(io.BytesIO(content))
    sections: list[tuple[str, str, dict[str, Any]]] = []
    current_title = "文档说明"
    lines: list[str] = []
    ordinal = 0

    def flush() -> None:
        nonlocal lines, ordinal
        text = "\n".join(lines).strip()
        if text:
            ordinal += 1
            for index, part in enumerate(_split_text(text, chunking_strategy), 1):
                chapter = current_title + (f"（片段 {index}）" if index > 1 else "")
                sections.append((chapter, part, {"section": ordinal, "part": index, "extraction": "docx"}))
        lines = []

    for paragraph in document.paragraphs:
        value = paragraph.text.strip()
        if paragraph.style and paragraph.style.name.startswith("Heading") and value:
            flush()
            current_title = value
        elif value:
            lines.append(value)
    flush()
    for table_index, table in enumerate(document.tables, 1):
        rows = ["\t".join(cell.text.strip() for cell in row.cells) for row in table.rows]
        text = "\n".join(row for row in rows if row.strip())
        if text:
            sections.append((f"表 {table_index}", text, {"table": table_index, "extraction": "docx_table"}))
    if not sections:
        raise ValueError("DOCX 没有可解析的正文或表格")
    card, chunks = _card(
        document_id=document_id, version=1, title=title, confidentiality=confidentiality,
        sections=sections, document_type="paper", chunking_strategy=chunking_strategy,
    )
    source = "\n\n".join(f"[{chapter}]\n{content}" for chapter, content, _ in sections)
    return ParsedInput(source, card, chunks, "paper")


def _tex(text: str, document_id: str, title: str, confidentiality: str, chunking_strategy: str = "auto") -> ParsedInput:
    text = re.sub(r"(?m)(?<!\\)%.*$", "", text)
    matches = list(re.finditer(r"\\(?:section|subsection|subsubsection)\*?\{([^{}]+)\}", text))
    sections: list[tuple[str, str, dict[str, Any]]] = []
    if not matches:
        sections = [("TeX 正文", text.strip(), {"extraction": "tex"})]
    else:
        if text[:matches[0].start()].strip():
            sections.append(("TeX 前言", text[:matches[0].start()].strip(), {"extraction": "tex"}))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            body = text[match.end():end].strip()
            if body:
                sections.append((match.group(1).strip(), body, {"section": match.group(1).strip(), "extraction": "tex"}))
    sections = [(chapter, part, {**locator, "part": part_index})
                for chapter, content, locator in sections if content
                for part_index, part in enumerate(_split_text(content, chunking_strategy), 1)]
    card, chunks = _card(
        document_id=document_id, version=1, title=title, confidentiality=confidentiality,
        sections=sections, document_type="paper", chunking_strategy=chunking_strategy,
    )
    return ParsedInput(text, card, chunks, "paper")


def _csv(content: bytes, document_id: str, title: str, confidentiality: str, chunking_strategy: str = "auto") -> ParsedInput:
    text = content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2:
        raise ValueError("CSV 至少需要表头和一行数据")
    header = rows[0]
    sections: list[tuple[str, str, dict[str, Any]]] = []
    for start in range(1, len(rows), 50):
        batch = rows[start:start + 50]
        lines = ["\t".join(header)] + ["\t".join(row) for row in batch]
        sections.append((f"CSV 第 {start + 1}–{start + len(batch)} 行", "\n".join(lines), {
            "row_start": start + 1, "row_end": start + len(batch), "extraction": "csv",
        }))
    card, chunks = _card(
        document_id=document_id, version=1, title=title, confidentiality=confidentiality,
        sections=sections, document_type="paper", chunking_strategy=chunking_strategy,
    )
    return ParsedInput(text, card, chunks, "paper")


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.title = ""
        self._ignored = 0
        self._title = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self._ignored += 1
        if tag == "title":
            self._title = True
        if tag in ("p", "div", "section", "article", "h1", "h2", "h3", "tr", "li", "br"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self._ignored:
            self._ignored -= 1
        if tag == "title":
            self._title = False
        if tag in ("p", "div", "section", "article", "h1", "h2", "h3", "tr", "li"):
            self.parts.append("\n")

    def handle_data(self, data):
        if self._title:
            self.title += data
        if not self._ignored:
            self.parts.append(data)


def parse_html_text(text: str) -> tuple[str, str]:
    parser = _HTMLText()
    parser.feed(text)
    lines = [re.sub(r"\s+", " ", line).strip() for line in "".join(parser.parts).splitlines()]
    return parser.title.strip(), "\n".join(line for line in lines if line)


def _html(text: str, document_id: str, title: str, confidentiality: str, chunking_strategy: str = "auto") -> ParsedInput:
    html_title, body = parse_html_text(text)
    if not body:
        raise ValueError("网页没有可解析的正文")
    sections = [(html_title or "网页正文", part, {"part": index, "extraction": "html"})
                for index, part in enumerate(_split_text(body, chunking_strategy), 1)]
    card, chunks = _card(
        document_id=document_id, version=1, title=html_title or title,
        confidentiality=confidentiality, sections=sections, document_type="paper", chunking_strategy=chunking_strategy,
    )
    return ParsedInput(body, card, chunks, "paper")


def parse_input(
    filename: str,
    content: bytes,
    *,
    document_id: str,
    title: str,
    confidentiality: str,
    document_type: str | None = None,
    chunking_strategy: str = "auto",
) -> ParsedInput:
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ValueError("P2 支持 PDF、DOCX、Markdown、TeX、CSV 和 HTML")
    if suffix in (".md", ".markdown"):
        card, chunks = parse_markdown(
            content.decode("utf-8-sig"), document_id=document_id, version=1,
            title=title, confidentiality=confidentiality, chunking_strategy=chunking_strategy,
        )
        for chunk in chunks:
            chunk["locator_json"] = {**chunk["locator_json"], "chapter": chunk["chapter"], "extraction": "markdown"}
        return ParsedInput(content.decode("utf-8-sig"), card, chunks, document_type or "tutorial")
    if suffix == ".pdf":
        return _pdf(content, document_id, title, confidentiality, chunking_strategy)
    if suffix == ".docx":
        return _docx(content, document_id, title, confidentiality, chunking_strategy)
    text = content.decode("utf-8-sig")
    if suffix == ".tex":
        return _tex(text, document_id, title, confidentiality, chunking_strategy)
    if suffix == ".csv":
        return _csv(content, document_id, title, confidentiality, chunking_strategy)
    return _html(text, document_id, title, confidentiality, chunking_strategy)
