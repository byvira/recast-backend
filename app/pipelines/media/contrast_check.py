"""Real WCAG 2.x contrast-ratio check for a rendered image asset.

Pure computation (the W3C relative-luminance/contrast-ratio formula) — no
external call, nothing to live-verify. Checks the exact color pairs
image_render.py's render_slide() actually draws: the headline text
(_DEFAULT_FG) against the solid brand background (brand_tokens.primary_hex),
and the accent-keyword highlight (brand_tokens.accent_hex) against that same
background — the real pairs, not a guessed one.
"""

from pydantic import BaseModel


def _relative_luminance(hex_color: str) -> float:
    hex_color = hex_color.lstrip("#")

    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(int(hex_color[i:i + 2], 16) / 255.0) for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(hex_a: str, hex_b: str) -> float:
    """The real W3C formula: (L1 + 0.05) / (L2 + 0.05), lighter over darker."""
    la, lb = _relative_luminance(hex_a), _relative_luminance(hex_b)
    lighter, darker = max(la, lb), min(la, lb)
    return round((lighter + 0.05) / (darker + 0.05), 2)


class ContrastResult(BaseModel):
    pair: str
    foreground: str
    background: str
    ratio: float
    passes_aa_normal_text: bool   # WCAG AA, normal text: >= 4.5
    passes_aa_large_text: bool    # WCAG AA, large/bold text: >= 3.0
    passes_aaa_normal_text: bool  # WCAG AAA, normal text: >= 7.0


def check_slide_contrast(headline_fg_hex: str, accent_hex: str, background_hex: str) -> list[ContrastResult]:
    results = []
    for name, fg in (("headline", headline_fg_hex), ("accent_keyword", accent_hex)):
        ratio = contrast_ratio(fg, background_hex)
        results.append(ContrastResult(
            pair=name, foreground=fg, background=background_hex, ratio=ratio,
            passes_aa_normal_text=ratio >= 4.5,
            passes_aa_large_text=ratio >= 3.0,
            passes_aaa_normal_text=ratio >= 7.0,
        ))
    return results
