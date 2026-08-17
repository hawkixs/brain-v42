"""Markdown chunker for project plans and specs.

Splits markdown content on H2 headers into chunks. Handles YAML frontmatter,
fenced code blocks (no header bleed), min/max chunk rules, and CRLF line endings.
Pure functions, no I/O, no DB.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import yaml

logger = logging.getLogger(__name__)

MIN_CHUNK_WORDS = 50
MAX_CHUNK_WORDS = 1500


@dataclass
class PlanParentData:
    title: str
    preamble: str
    content: str
    summary: str | None = None
    status: str = "active"
    word_count: int = 0
    tags: list[str] = field(default_factory=list)


@dataclass
class ChunkData:
    section_title: str
    section_path: str
    content: str
    section_order: int
    word_count: int = 0


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_H1_RE = re.compile(r"^# (?!#)(.+?)\s*$", re.MULTILINE)
_FRONTMATTER_STATUS_ALIASES = {
    "completed": "archived",
    "planned": "active",
}


def _count_words(text: str) -> int:
    return len(text.split())


def _normalize_line_endings(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _extract_frontmatter(content: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, content_without_frontmatter)."""
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return {}, content
    try:
        data = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        logger.warning("Failed to parse plan frontmatter; ignoring.")
        return {}, content[match.end() :]
    return data, content[match.end() :]


def _strip_fenced_code_blocks(content: str) -> str:
    """Replace fenced code block content with blank lines of equal length.

    Preserves byte offsets so that header positions found in the scrubbed
    string are valid offsets into the original content.
    """
    out: list[str] = []
    in_fence = False
    for line in content.split("\n"):
        stripped = line.lstrip()
        is_fence_marker = stripped.startswith("```") or stripped.startswith("~~~")
        if is_fence_marker:
            in_fence = not in_fence
            out.append(" " * len(line))
            continue
        out.append(" " * len(line) if in_fence else line)
    return "\n".join(out)


def _extract_title_from_content(content: str) -> str:
    match = _H1_RE.search(content)
    return match.group(1).strip() if match else ""


def _find_h2_positions(scrubbed: str) -> list[tuple[int, int, str]]:
    """Return [(start, end_of_header_line, title), ...] for each H2."""
    positions: list[tuple[int, int, str]] = []
    for line_match in re.finditer(r"^## (?!#)(.+?)\s*$", scrubbed, re.MULTILINE):
        positions.append((line_match.start(), line_match.end(), line_match.group(1).strip()))
    return positions


def _build_chunks(
    content: str, scrubbed: str, h2_positions: list[tuple[int, int, str]]
) -> list[ChunkData]:
    chunks: list[ChunkData] = []
    for i, (start, _end, title) in enumerate(h2_positions):
        body_end = h2_positions[i + 1][0] if i + 1 < len(h2_positions) else len(content)
        section_body = content[start:body_end].rstrip("\n")
        # Collect H3s within this section to build a section path
        h3_titles = re.findall(r"^### (?!#)(.+?)\s*$", section_body, re.MULTILINE)
        path = " > ".join([title] + h3_titles) if h3_titles else title
        chunks.append(
            ChunkData(
                section_title=title,
                section_path=path,
                content=section_body,
                section_order=i,
                word_count=_count_words(section_body),
            )
        )
    return chunks


def _apply_min_max_rules(chunks: list[ChunkData]) -> list[ChunkData]:
    # Merge tiny chunks forward — only when the NEXT chunk itself is >= MIN_CHUNK_WORDS.
    # This avoids merging when all sections are small (e.g. short docs).
    merged: list[ChunkData] = []
    pending: ChunkData | None = None
    for chunk in chunks:
        if pending is not None:
            if chunk.word_count >= MIN_CHUNK_WORDS:
                chunk = ChunkData(
                    section_title=chunk.section_title,
                    section_path=chunk.section_path,
                    content=f"{pending.content}\n\n{chunk.content}",
                    section_order=chunk.section_order,
                    word_count=pending.word_count + chunk.word_count,
                )
                pending = None
            else:
                # Next chunk is also tiny — flush pending as-is and continue
                merged.append(pending)
                pending = None
        if chunk.word_count < MIN_CHUNK_WORDS:
            pending = chunk
            continue
        merged.append(chunk)
    # If the last chunk was tiny and had nothing to merge into, keep it
    if pending is not None:
        merged.append(pending)

    # Warn on oversized chunks (keep them as-is)
    for chunk in merged:
        if chunk.word_count > MAX_CHUNK_WORDS:
            logger.warning(
                "Oversized chunk detected: section=%r words=%d",
                chunk.section_title,
                chunk.word_count,
            )

    # Reassign section_order after merges
    for i, chunk in enumerate(merged):
        chunk.section_order = i
    return merged


def chunk_markdown(content: str) -> tuple[PlanParentData, list[ChunkData]]:
    """Split markdown into (parent, chunks)."""
    if not content:
        return PlanParentData(title="", preamble="", content=""), []

    content = _normalize_line_endings(content)
    fm, content_no_fm = _extract_frontmatter(content)

    # Title: frontmatter first, then first H1 in content
    title = str(fm.get("title") or fm.get("name") or "").strip()
    if not title:
        title = _extract_title_from_content(content_no_fm)

    raw_status = str(fm["status"] if "status" in fm else "active").strip()
    status = _FRONTMATTER_STATUS_ALIASES.get(raw_status, raw_status)
    summary = fm.get("summary")
    tags = fm.get("tags") or []
    if not isinstance(tags, list):
        tags = []

    scrubbed = _strip_fenced_code_blocks(content_no_fm)
    h2_positions = _find_h2_positions(scrubbed)

    if not h2_positions:
        preamble = content_no_fm
        chunks: list[ChunkData] = []
    else:
        preamble_raw = content_no_fm[: h2_positions[0][0]]
        preamble_lines = [line for line in preamble_raw.splitlines() if not line.startswith("# ")]
        preamble = "\n".join(preamble_lines).strip()
        chunks = _build_chunks(content_no_fm, scrubbed, h2_positions)
        chunks = _apply_min_max_rules(chunks)

    parent = PlanParentData(
        title=title,
        preamble=preamble,
        content=content_no_fm,
        summary=str(summary).strip() if summary else None,
        status=status,
        word_count=_count_words(content_no_fm),
        tags=[str(t) for t in tags],
    )
    return parent, chunks
