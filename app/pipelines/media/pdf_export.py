"""Real per-generation PDF export for the text pipeline — closes DEF-031
(`TextPipelineResult.pdf_export_url` was declared and threaded through
`GenerateTextRequest.extras.pdf_export`/`_build_metadata`, but nothing
anywhere ever wrote to it). One PDF per generation run, one section per
platform's piece — mirrors `app/api/v1/content.py::_pieces_to_markdown`'s
per-piece layout (platform/brand/created-at header, then content, then
hashtags), just rendered as a real document instead of a download-only
Markdown string, since this is a *persisted* url on the result, not a
one-off HTTP response.
"""

import os
from datetime import datetime, timezone

from fpdf import FPDF
from fpdf.enums import XPos, YPos

from app.models.text import GeneratedPiece

# Real Unicode font (Poppins, bundled for the Image pipeline's own render
# service — see app/pipelines/media/fonts/) instead of FPDF's core
# Helvetica, which is latin-1-only and raises on an em dash, a curly
# quote, or any non-Latin script. Reusing the same bundled files avoids a
# second font-sourcing decision for what's the same underlying need.
_FONTS_DIR = os.path.join(os.path.dirname(__file__), "fonts")


def _platform_label(piece: GeneratedPiece) -> str:
    return piece.platform.value if hasattr(piece.platform, "value") else str(piece.platform)


def _new_pdf() -> FPDF:
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_font("Poppins", "", os.path.join(_FONTS_DIR, "Poppins-Regular.ttf"))
    pdf.add_font("Poppins", "B", os.path.join(_FONTS_DIR, "Poppins-Bold.ttf"))
    pdf.add_font("Poppins", "I", os.path.join(_FONTS_DIR, "Poppins-Regular.ttf"))
    pdf.add_page()
    return pdf


def _line(pdf: FPDF, h: float, text: str) -> None:
    """multi_cell wrapper that always resets the cursor to the left
    margin afterward. fpdf2's own multi_cell default (new_x=XPos.RIGHT)
    leaves x at the cell's right edge instead — every next call inherits
    almost no horizontal space left on the page and raises
    FPDFException("Not enough horizontal space...") on its very first
    character. Confirmed by reproducing it directly against this
    project's installed fpdf2==2.8.8, not assumed from docs."""
    pdf.multi_cell(0, h, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def generate_pieces_pdf(pieces: list[GeneratedPiece], brand_name: str = "") -> bytes:
    """Renders a real PDF with one section per piece that has content
    (empty/failed pieces are skipped — nothing useful to export for
    them). Returns raw PDF bytes, uploaded by the caller."""
    pdf = _new_pdf()

    pdf.set_font("Poppins", "B", 16)
    title = f"{brand_name} - Recast Export" if brand_name else "Recast Export"
    _line(pdf, 10, title)
    pdf.set_font("Poppins", "", 10)
    pdf.set_text_color(120, 120, 120)
    _line(pdf, 6, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    pdf.set_text_color(0, 0, 0)
    pdf.ln(4)

    real_pieces = [p for p in pieces if p.content.strip()]
    if not real_pieces:
        pdf.set_font("Poppins", "", 12)
        _line(pdf, 8, "No content was generated in this run.")
        return bytes(pdf.output())

    for piece in real_pieces:
        pdf.set_font("Poppins", "B", 13)
        _line(pdf, 9, _platform_label(piece))
        pdf.set_font("Poppins", "", 11)
        _line(pdf, 6, piece.content)

        hashtags = (piece.seo or {}).get("hashtags") or []
        if hashtags:
            pdf.set_font("Poppins", "I", 9)
            pdf.set_text_color(90, 90, 90)
            _line(pdf, 6, " ".join(f"#{t}" for t in hashtags))
            pdf.set_text_color(0, 0, 0)

        pdf.ln(6)

    return bytes(pdf.output())
