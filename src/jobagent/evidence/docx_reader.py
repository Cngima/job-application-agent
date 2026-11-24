"""Read a .docx into an ordered list of text lines, including table cells.

python-docx's `Document.paragraphs` skips tables, and CVs often keep competencies in a
table, so we walk the body XML in document order instead.
"""

from __future__ import annotations

from pathlib import Path

import docx

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _paragraph_text(p_el) -> str:
    parts: list[str] = []
    for node in p_el.iter():
        if node.tag == f"{_W}t" and node.text:
            parts.append(node.text)
        elif node.tag == f"{_W}tab":
            parts.append("\t")
    return "".join(parts)


def read_lines(path: Path | str) -> list[str]:
    """Return non-empty lines in document order. Each table cell paragraph is its own line."""
    body = docx.Document(str(path)).element.body
    lines: list[str] = []
    for el in body.iterchildren():
        if el.tag == f"{_W}p":
            text = _paragraph_text(el).strip()
            if text:
                lines.append(text)
        elif el.tag == f"{_W}tbl":
            for p_el in el.iter(f"{_W}p"):
                text = _paragraph_text(p_el).strip()
                if text:
                    lines.append(text)
    return lines
