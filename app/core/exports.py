"""Spreadsheet (CSV) exports from Ops: one helper, so every export opens cleanly in Excel or Sheets and is safe to open.

A cell that starts with =, +, - or @ is written with a leading apostrophe, so a spreadsheet never runs it as a formula (text a customer
typed could otherwise run when Ops opens the file). Dates are written as plain dates and times in UTC.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any, Iterable

from fastapi.responses import Response

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return value.strftime("%Y-%m-%d %H:%M UTC")
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".") if value != int(value) else str(int(value))
    text = str(value)
    if text.startswith(_FORMULA_START):
        return "'" + text
    return text


def csv_response(filename: str, head: list[str], rows: Iterable[Iterable[Any]]) -> Response:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(head)
    for row in rows:
        writer.writerow([_cell(v) for v in row])
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # the byte-order mark lets Excel read names in any language correctly
    return Response(
        content="\ufeff" + buffer.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}-{stamp}.csv"', "Cache-Control": "no-store"},
    )
