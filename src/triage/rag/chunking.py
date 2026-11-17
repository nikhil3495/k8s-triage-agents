"""Split runbooks into section-level chunks carrying their front-matter metadata."""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

_FRONT_MATTER = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_SECTION = re.compile(r"^## +(.+)$", re.MULTILINE)


class Chunk(BaseModel):
    """One retrievable unit: a runbook section."""

    chunk_id: str
    runbook_id: str
    title: str
    category: str
    section: str
    text: str
    symptoms: list[str] = Field(default_factory=list)

    def search_text(self) -> str:
        """Text used for lexical and dense indexing (title + symptoms + section body)."""
        return f"{self.title}. {self.section}. {' '.join(self.symptoms)}\n{self.text}"


def parse_runbook(path: Path, max_chars: int = 1800) -> list[Chunk]:
    """Parse one runbook file into chunks; long sections are split on blank lines."""
    raw = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    meta: dict[str, object] = {}
    m = _FRONT_MATTER.match(raw)
    if m:
        meta = yaml.safe_load(m.group(1)) or {}
        raw = raw[m.end() :]
    runbook_id = str(meta.get("id") or path.stem)
    title = str(meta.get("title") or runbook_id)
    category = str(meta.get("category") or "unknown")
    symptoms = [str(s) for s in meta.get("symptoms") or []]  # type: ignore[attr-defined]

    headers = list(_SECTION.finditer(raw))
    chunks: list[Chunk] = []
    for i, h in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(raw)
        body = raw[h.end() : end].strip()
        if not body:
            continue
        section = h.group(1).strip()
        parts: list[str] = []
        buf = ""
        for para in body.split("\n\n"):
            if buf and len(buf) + len(para) > max_chars:
                parts.append(buf)
                buf = para
            else:
                buf = f"{buf}\n\n{para}" if buf else para
        if buf:
            parts.append(buf)
        slug = re.sub(r"[^a-z0-9]+", "-", section.lower()).strip("-")
        for j, text in enumerate(parts):
            chunks.append(
                Chunk(
                    chunk_id=f"{runbook_id}#{slug}" + (f"-{j}" if len(parts) > 1 else ""),
                    runbook_id=runbook_id,
                    title=title,
                    category=category,
                    section=section,
                    text=text,
                    symptoms=symptoms,
                )
            )
    return chunks


def load_corpus(runbooks_dir: Path) -> list[Chunk]:
    """Chunk every ``*.md`` runbook in a directory (sorted for determinism)."""
    out: list[Chunk] = []
    for path in sorted(runbooks_dir.glob("*.md")):
        out.extend(parse_runbook(path))
    return out
