import re
from dataclasses import dataclass
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from django.apps import apps
from django.conf import settings
from netbox.plugins import PluginConfig

MAX_DOCUMENT_FILES = 1000
MAX_DOCUMENT_BYTES = 1_000_000
MAX_SECTION_CHARS = 6000
WORD_RE = re.compile(r"[\w.-]{2,}", re.UNICODE)
MARKDOWN_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$")


@dataclass(frozen=True, slots=True)
class DocumentationSection:
    doc_id: str
    source: str
    title: str
    section: str
    content: str
    url: str | None


class _HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "nav"}:
            self.skip_depth += 1
        elif not self.skip_depth and tag in {"h1", "h2", "h3", "h4", "p", "li", "pre", "br"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "nav"} and self.skip_depth:
            self.skip_depth -= 1
        elif not self.skip_depth and tag in {"h1", "h2", "h3", "h4", "p", "li", "pre"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        return "\n".join(line.strip() for line in "".join(self.parts).splitlines() if line.strip())


class DocumentationIndex:
    """Bounded local search over the installed NetBox and plugin documentation."""

    def __init__(self, config: dict[str, Any] | None = None):
        config = config or {}
        self.max_results = max(1, min(int(config.get("max_results", 5)), 10))
        self.max_section_chars = max(1000, min(int(config.get("max_section_chars", 12000)), 30000))
        root_specs = self._root_specs(config.get("additional_roots") or [])
        self._sections = _build_sections(root_specs)
        self._by_id = {section.doc_id: section for section in self._sections}

    @property
    def available(self) -> bool:
        return bool(self._sections)

    def search(self, query: str, limit: int | None = None) -> list[dict[str, Any]]:
        normalized_query = " ".join(query.casefold().split())
        terms = tuple(dict.fromkeys(WORD_RE.findall(normalized_query)))
        if not terms:
            return []
        scored = []
        for section in self._sections:
            title = section.title.casefold()
            heading = section.section.casefold()
            content = section.content.casefold()
            coverage = sum(term in f"{title} {heading} {content}" for term in terms)
            if not coverage:
                continue
            score = coverage * 20
            score += 40 if normalized_query in title else 0
            score += 30 if normalized_query in heading else 0
            score += sum(12 for term in terms if term in title)
            score += sum(8 for term in terms if term in heading)
            score += sum(min(content.count(term), 5) for term in terms)
            scored.append((score, coverage, section))
        scored.sort(key=lambda item: (-item[0], -item[1], item[2].source, item[2].title, item[2].section))
        bounded_limit = max(1, min(limit or self.max_results, self.max_results))
        return [self._search_result(section, normalized_query, terms) for _, _, section in scored[:bounded_limit]]

    def read(self, doc_id: str) -> dict[str, Any] | None:
        section = self._by_id.get(doc_id)
        if section is None:
            return None
        return {
            "doc_id": section.doc_id,
            "source": section.source,
            "title": section.title,
            "section": section.section,
            "content": section.content[: self.max_section_chars],
            "truncated": len(section.content) > self.max_section_chars,
            "url": section.url,
        }

    def _search_result(self, section: DocumentationSection, query: str, terms: tuple[str, ...]) -> dict[str, Any]:
        content_folded = section.content.casefold()
        positions = [content_folded.find(value) for value in (query, *terms)]
        positions = [position for position in positions if position >= 0]
        start = max(0, (min(positions) if positions else 0) - 180)
        end = min(len(section.content), start + 900)
        snippet = section.content[start:end].strip()
        if start:
            snippet = f"…{snippet}"
        if end < len(section.content):
            snippet = f"{snippet}…"
        return {
            "doc_id": section.doc_id,
            "source": section.source,
            "title": section.title,
            "section": section.section,
            "snippet": snippet,
            "url": section.url,
        }

    @staticmethod
    def _root_specs(additional_roots: list[str]) -> tuple[tuple[str, str, str | None], ...]:
        specs: list[tuple[str, str, str | None]] = []
        docs_root = Path(settings.DOCS_ROOT).resolve()
        specs.append(("netbox", str(docs_root), "netbox"))

        for app_config in apps.get_app_configs():
            if not isinstance(app_config, PluginConfig):
                continue
            source = f"plugin:{app_config.label}"
            app_path = Path(app_config.path).resolve()
            for candidate in (app_path / "docs", app_path.parent / "docs"):
                if candidate.is_dir():
                    specs.append((source, str(candidate), None))
                    break
            for candidate in (app_path / "README.md", app_path.parent / "README.md"):
                if candidate.is_file():
                    specs.append((source, str(candidate), None))
                    break

        for index, value in enumerate(additional_roots):
            path = Path(value).expanduser().resolve()
            if path.exists():
                specs.append((f"additional:{index + 1}", str(path), None))
        return tuple(dict.fromkeys(specs))


@lru_cache(maxsize=8)
def _build_sections(root_specs: tuple[tuple[str, str, str | None], ...]) -> tuple[DocumentationSection, ...]:
    sections: list[DocumentationSection] = []
    seen_files: set[Path] = set()
    for source, root_value, url_kind in root_specs:
        root = Path(root_value).resolve()
        root_is_file = root.is_file()
        files = [root] if root_is_file else sorted(root.rglob("*.md"))
        if not files and root.is_dir():
            files = sorted(root.rglob("*.html"))
        for path in files:
            resolved = path.resolve()
            if not root_is_file and not resolved.is_relative_to(root):
                continue
            if resolved in seen_files or not path.is_file() or len(seen_files) >= MAX_DOCUMENT_FILES:
                continue
            seen_files.add(resolved)
            try:
                if path.stat().st_size > MAX_DOCUMENT_BYTES:
                    continue
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            relative = path.name if root.is_file() else path.relative_to(root).as_posix()
            url = _documentation_url(relative) if url_kind == "netbox" else None
            sections.extend(_parse_document(source, relative, raw, path.suffix.casefold(), url))
    return tuple(sections)


def _parse_document(
    source: str,
    relative: str,
    raw: str,
    suffix: str,
    url: str | None,
) -> list[DocumentationSection]:
    if suffix == ".html":
        parser = _HTMLTextExtractor()
        parser.feed(raw)
        raw = parser.text()
        title = Path(relative).parent.name.replace("-", " ").title() or Path(relative).stem.title()
        chunks = [(title, raw)]
    else:
        lines = raw.splitlines()
        title = next(
            (
                match.group(2).strip()
                for line in lines
                if (match := MARKDOWN_HEADING_RE.match(line)) and len(match.group(1)) == 1
            ),
            Path(relative).stem.replace("-", " ").title(),
        )
        chunks = _markdown_sections(lines, title)

    result = []
    for index, (heading, content) in enumerate(chunks):
        for part_index, part in enumerate(_bounded_chunks(content, MAX_SECTION_CHARS)):
            doc_id = f"{source}:{relative}:{index}:{part_index}"
            result.append(DocumentationSection(doc_id, source, title, heading, part, url))
    return result


def _markdown_sections(lines: list[str], title: str) -> list[tuple[str, str]]:
    sections = []
    heading = title
    content: list[str] = []
    for line in lines:
        match = MARKDOWN_HEADING_RE.match(line)
        if match and len(match.group(1)) <= 3:
            if any(value.strip() for value in content):
                sections.append((heading, "\n".join(content).strip()))
            heading = match.group(2).strip()
            content = []
        else:
            content.append(line)
    if any(value.strip() for value in content):
        sections.append((heading, "\n".join(content).strip()))
    return sections or [(title, "\n".join(lines).strip())]


def _bounded_chunks(content: str, size: int) -> list[str]:
    if len(content) <= size:
        return [content]
    paragraphs = re.split(r"\n\s*\n", content)
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if current and len(current) + len(paragraph) + 2 > size:
            chunks.append(current)
            current = ""
        while len(paragraph) > size:
            chunks.append(paragraph[:size])
            paragraph = paragraph[size:]
        current = f"{current}\n\n{paragraph}".strip()
    if current:
        chunks.append(current)
    return chunks


def _documentation_url(relative: str) -> str | None:
    static_url = str(settings.STATIC_URL)
    if not static_url.startswith("/") or static_url.startswith("//"):
        return None
    static_docs_available = any(
        isinstance(value, (tuple, list)) and len(value) == 2 and value[0] == "docs" and Path(value[1]).is_dir()
        for value in settings.STATICFILES_DIRS
    )
    if not static_docs_available:
        return None
    path = relative.removesuffix(".md").removesuffix(".html").strip("/")
    if path.endswith("/index"):
        path = path.removesuffix("/index")
    return f"{static_url.rstrip('/')}/docs/{path}/"
