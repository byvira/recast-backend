"""Reads the text out of a document a member drops in as a campaign source: plain text, Markdown, Word (.docx) and PDF.

The writing pipeline reads about 8,000 characters of a source, so a long document is condensed into evenly spaced excerpts from the
whole of it (the same way a long recording is), not just its first pages."""
from __future__ import annotations

import io
import re
from typing import Any

from app.pipelines.campaigns.sources import SourceError, condense

MAX_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 300
SUPPORTED = (".txt", ".md", ".markdown", ".docx", ".pdf")


def _extension(filename: str) -> str:
    match = re.search(r"\.[A-Za-z0-9]+$", (filename or "").strip())
    return match.group(0).lower() if match else ""


def _text_file(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return data.decode("utf-8", errors="ignore")


def _docx(data: bytes) -> str:
    try:
        from docx import Document

        document = Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise SourceError("That Word file couldn't be opened. Save it again as .docx and try again.") from exc
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise SourceError("That PDF is locked with a password. Remove the password and try again.")
        pages = reader.pages[:MAX_PDF_PAGES]
        return "\n".join((page.extract_text() or "") for page in pages)
    except SourceError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SourceError("That PDF couldn't be read. Try a different copy of it.") from exc


def read_source_file(filename: str, data: bytes) -> dict[str, Any]:
    """{"title", "text", "characters", "condensed"} for a document, or a SourceError saying what is wrong."""
    extension = _extension(filename)
    if extension not in SUPPORTED:
        if extension in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
            raise SourceError("A video file can't be used as a source here yet. Add its audio in the Audio pipeline, or paste a transcript.")
        raise SourceError("Use a text, Markdown, Word (.docx) or PDF file. For a recording, upload the audio instead.")
    if not data:
        raise SourceError("That file is empty.")
    if len(data) > MAX_BYTES:
        raise SourceError(f"That file is larger than {MAX_BYTES // (1024 * 1024)} MB. Split it and use a part.")

    raw = _text_file(data) if extension in (".txt", ".md", ".markdown") else _docx(data) if extension == ".docx" else _pdf(data)
    full = re.sub(r"[ \t]+", " ", raw)
    full = re.sub(r"\n{3,}", "\n\n", full).strip()
    if len(full) < 20:
        raise SourceError("There is hardly any text in that file. A scanned PDF or a picture of text can't be read here.")
    text = condense(full)
    title = re.sub(r"\.[A-Za-z0-9]+$", "", (filename or "Document").strip())[:80] or "Document"
    return {"title": title, "text": text, "characters": len(full), "condensed": len(re.sub(r"\s+", " ", full)) > len(text)}
