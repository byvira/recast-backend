"""Reading a file a member gives as a source: PDF, Word and CSV (plain text and Markdown too).

The kind is decided by what the file really is (its first bytes), never by its name, so a renamed program or an image called `notes.pdf` is
refused. Files are read in memory, never run and never kept; a size limit applies before anything is opened. PDF, Word and text go through the
readers the campaign sources already use; CSV is read here, one row per item.

Excel is read with `openpyxl` in read-only mode: the first sheet only, values only (no formula is run, no macro is opened), with the same row, column and
size limits as CSV and a check on how large the file is once unpacked."""
from __future__ import annotations

import csv
import io
import re
from typing import Optional

from app.pipelines.campaigns.source_files import SourceError, read_source_file
from app.sources.items import MAX_TITLE, Item, ReadError, item_id, plain

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 5000
MAX_COLUMNS = 40
MAX_CELL = 4000

PDF, DOCX, XLSX, CSV, TEXT, IMAGE, VIDEO, AUDIO, OTHER = "pdf", "docx", "xlsx", "csv", "text", "image", "video", "audio", "other"


def sniff(data: bytes, filename: str = "") -> str:
    """What the file really is. A zip is looked inside for a Word or Excel document; text is told from binary by a null-byte test."""
    head = data[:16]
    if head.startswith(b"%PDF"):
        return PDF
    if head.startswith(b"PK\x03\x04"):
        try:
            import zipfile

            names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        except Exception:  # noqa: BLE001
            return OTHER
        if any(n.startswith("word/") for n in names):
            return DOCX
        if any(n.startswith("xl/") for n in names):
            return XLSX
        return OTHER
    if head.startswith((b"\xff\xd8\xff", b"\x89PNG", b"GIF8")) or (head[:4] == b"RIFF" and data[8:12] == b"WEBP"):
        return IMAGE
    if data[4:8] == b"ftyp" or head.startswith((b"\x1a\x45\xdf\xa3", b"OggS")) and filename.lower().endswith((".webm", ".mkv", ".ogv")):
        return VIDEO
    if head.startswith((b"ID3", b"\xff\xfb", b"\xff\xf3", b"fLaC", b"RIFF")):
        return AUDIO
    if b"\x00" in data[:4096] and not data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return OTHER
    name = filename.lower()
    return CSV if name.endswith((".csv", ".tsv")) else TEXT


def _decode(data: bytes) -> str:
    """Text from bytes. UTF-16 only when the file says so (its byte-order mark): without that, any even run of Windows-encoded bytes also "decodes" as UTF-16, as garbage."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeError:
            pass
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return data.decode("utf-8", errors="ignore")


def _looks_like_header(first: list[str]) -> bool:
    """A first row of words, none empty, none a plain number, none repeated, is a header. (Python's own guess fails when every column is words.)"""
    cells = [c.strip() for c in first]
    if len(cells) < 2 or not all(cells):
        return False
    if any(re.fullmatch(r"[-+]?[\d.,]+%?", c) for c in cells):
        return False
    return len({c.lower() for c in cells}) == len(cells)


def _table(rows: list[list[str]], title_column: Optional[str], text_columns: Optional[list[str]], kind: str) -> tuple[list[str], list[Item]]:
    """(the column names, one item per row) from rows of text. The first row is the header when it looks like one; otherwise the columns are named Column 1, 2 and so on.
    The title column and the text columns default to the first column and all the others."""
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise ReadError("empty", "That file has no rows.")
    if len(rows[0]) > MAX_COLUMNS:
        raise ReadError("too_wide", f"That file has more than {MAX_COLUMNS} columns, which is too many for a source.")
    has_header = _looks_like_header(rows[0])
    header = [c.strip() or f"Column {i + 1}" for i, c in enumerate(rows[0])] if has_header else [f"Column {i + 1}" for i in range(len(rows[0]))]
    body = rows[1:] if has_header else rows
    if len(body) > MAX_ROWS:
        raise ReadError("too_many_rows", f"That file has more than {MAX_ROWS:,} rows. Use a part of it.")
    title_at = header.index(title_column) if title_column in header else 0
    text_at = [header.index(c) for c in (text_columns or []) if c in header] or [i for i in range(len(header)) if i != title_at]
    items: list[Item] = []
    for number, row in enumerate(body, start=1):
        cell = lambda i: (row[i].strip()[:MAX_CELL] if i < len(row) else "")  # noqa: E731
        name = plain(cell(title_at), MAX_TITLE) or f"Row {number}"
        content = "\n".join(f"{header[i]}: {cell(i)}" for i in text_at if cell(i))
        if not (cell(title_at) or content):
            continue
        items.append(Item(id=item_id("", f"row:{number}:{name}", name), title=name, text=content, kind=kind, extra={"row": number}))
    return header, items


def read_csv(data: bytes, *, title_column: Optional[str] = None, text_columns: Optional[list[str]] = None) -> tuple[list[str], list[Item]]:
    """(the column names, one item per row) for a CSV."""
    text = _decode(data)
    if not text.strip():
        raise ReadError("empty", "That file is empty.")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return _table(list(csv.reader(io.StringIO(text), dialect)), title_column, text_columns, "csv")


#: an Excel file is a zip; one that unpacks to more than this is refused before anything is read (a "zip bomb")
MAX_UNPACKED = 80 * 1024 * 1024


def _cell_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def read_xlsx(data: bytes, *, title_column: Optional[str] = None, text_columns: Optional[list[str]] = None) -> tuple[list[str], list[Item]]:
    """(the column names, one item per row) for the first sheet of an Excel file."""
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if sum(i.file_size for i in archive.infolist()) > MAX_UNPACKED:
                raise ReadError("too_large", "That Excel file is too large once opened. Save a smaller part of it and use that.")
    except zipfile.BadZipFile as exc:
        raise ReadError("cant_read", "That Excel file could not be opened.") from exc
    try:
        from openpyxl import load_workbook

        book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            sheet = book.worksheets[0]
            rows: list[list[str]] = []
            for row in sheet.iter_rows(values_only=True, max_row=MAX_ROWS + 2, max_col=MAX_COLUMNS + 1):
                cells = [_cell_text(c) for c in row]
                while cells and not cells[-1]:                      # the reader pads every row to the width asked for; only real columns count
                    cells.pop()
                rows.append(cells)
        finally:
            book.close()
    except ReadError:
        raise
    except Exception as exc:  # noqa: BLE001 - a damaged or unusual workbook is a plain refusal, never a crash
        raise ReadError("cant_read", "That Excel file could not be read. Save it again as .xlsx or as CSV and try once more.") from exc
    return _table(rows, title_column, text_columns, "xlsx")


def read(filename: str, data: bytes, *, title_column: Optional[str] = None, text_columns: Optional[list[str]] = None) -> tuple[str, list[Item]]:
    """(a title, items) for a file. One item for a PDF, Word or text document; one per row for a CSV. Raises `ReadError`."""
    if not data:
        raise ReadError("empty", "That file is empty.")
    if len(data) > MAX_BYTES:
        raise ReadError("too_large", f"That file is larger than {MAX_BYTES // (1024 * 1024)} MB. Split it and use a part.")
    kind = sniff(data, filename)
    if kind == CSV:
        _, items = read_csv(data, title_column=title_column, text_columns=text_columns)
        return re.sub(r"\.[A-Za-z0-9]+$", "", filename or "Spreadsheet")[:80] or "Spreadsheet", items
    if kind == XLSX:
        _, items = read_xlsx(data, title_column=title_column, text_columns=text_columns)
        return re.sub(r"\.[A-Za-z0-9]+$", "", filename or "Spreadsheet")[:80] or "Spreadsheet", items
    if kind in (IMAGE, VIDEO, AUDIO):
        raise ReadError(f"{kind}_not_here", {IMAGE: "A picture is read in the picture step, not as a document.", VIDEO: "A video is read in the video step, not as a document.",
                                              AUDIO: "A recording is read in the audio step, not as a document."}[kind])
    if kind == OTHER:
        raise ReadError("unsupported", "That kind of file cannot be read. Use a PDF, Word (.docx), CSV, text or Markdown file.")
    # PDF, Word and text: the readers the campaign sources already use, given a name that matches what the file really is
    safe_name = {PDF: "source.pdf", DOCX: "source.docx", TEXT: "source.txt"}[kind]
    try:
        doc = read_source_file(safe_name, data)
    except SourceError as exc:
        raise ReadError("cant_read", str(exc)) from exc
    title = re.sub(r"\.[A-Za-z0-9]+$", "", (filename or "Document").strip())[:80] or "Document"
    return title, [Item(id=item_id("", f"file:{title}:{len(data)}", title), title=title, text=doc["text"], kind=kind, extra={"characters": doc["characters"], "condensed": doc["condensed"]})]


def columns_of(filename: str, data: bytes) -> list[str]:
    """The column names of a table file (CSV or Excel), so a member can choose the title and text columns. Empty for any other kind."""
    kind = sniff(data, filename)
    if kind == CSV:
        return read_csv(data)[0]
    if kind == XLSX:
        return read_xlsx(data)[0]
    return []
