"""Real server-side render/compositing service for the ImageAsset pipeline —
see pow/audio_image_pipeline/01-image-pipeline-plan.md. Generalizes
default_image.py::render_quote_card's proven technique (real Pillow text
render, brand colors, PNG via BytesIO) from "one hardcoded quote-card
layout" to "layout preset + brand tokens + text -> composited PNG".

Supports all 9 layouts as of 2026-09-26 (bugs/gaps sweep, part 2): the
render logic itself was always layout-agnostic (just `LAYOUT_DIMS[layout]`);
5 already had a real, UI-confirmed pixel target (quote_1_1/carousel_4_5/
story_9_16/hero_16_9/podcast_cover); the remaining 4 (bento/infographic/
chart/code_snippet) had none declared anywhere in the UI, so their
dimensions were a real product decision, not something to guess — the user
picked reasonable defaults explicitly (see PROGRESS.md's Decisions Log
entry for the exact pixel targets and rationale) rather than have them
invented silently.

Deliberately does NOT accept effects (drop-shadow/glow, dual-tone
gradient, grid texture) or badges (author lockup, verified stamp, review
badge, sticky note) — those are real Phase-2 UI toggles the plan
explicitly defers, and a function that accepted those params without
actually implementing them would be exactly the "fake depth" this
build's own rules forbid. Add them here, for real, when Phase 2 is
picked up — not stubbed now.

Real brand typography as of 2026-09-26: Google Fonts .ttf files (see
`app/pipelines/media/fonts/` and `_font_for_profile()` below), replacing
the earlier Pillow-default-only limitation. Falls back to Pillow's
default font only if a font file is missing on disk (defensive, not the
expected path).
"""

import logging
import os
from io import BytesIO
from typing import Optional

from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont
from pydantic import BaseModel

from app.models.image_asset import LayoutPreset
from app.pipelines.media.icons import DEFAULT_ACCENT_ICON, icon_char, is_known_icon, load_icon_font

logger = logging.getLogger(__name__)

_FONTS_DIR = os.path.join(os.path.dirname(__file__), "fonts")

# Curated, bundled Google Fonts (real .ttf files under ./fonts/, OFL/Apache
# licensed — safe to bundle and ship) — real typography, 2026-09-26 (bugs/
# gaps sweep, part 2), replacing the earlier Pillow-default-only
# limitation. `VisualIdentity.fonts.heading`/`.body` are free-text on the
# brand ("e.g. Inter, Poppins…" — see the Brand Assets tab), so this maps
# the user's typed family name to a real bundled file rather than
# supporting literally any font on earth. Keys are lowercased for
# case-insensitive matching against the user's typed text.
_GOOGLE_FONTS: dict[str, dict[str, str]] = {
    "inter": {"regular": "Inter.ttf", "bold": "Inter.ttf"},
    "poppins": {"regular": "Poppins-Regular.ttf", "bold": "Poppins-Bold.ttf"},
    "roboto": {"regular": "Roboto.ttf", "bold": "Roboto.ttf"},
    "montserrat": {"regular": "Montserrat.ttf", "bold": "Montserrat.ttf"},
    "playfair display": {"regular": "PlayfairDisplay.ttf", "bold": "PlayfairDisplay.ttf"},
    "lora": {"regular": "Lora.ttf", "bold": "Lora.ttf"},
    "merriweather": {"regular": "Merriweather.ttf", "bold": "Merriweather.ttf"},
    "oswald": {"regular": "Oswald.ttf", "bold": "Oswald.ttf"},
    "raleway": {"regular": "Raleway.ttf", "bold": "Raleway.ttf"},
    "open sans": {"regular": "OpenSans.ttf", "bold": "OpenSans.ttf"},
    "work sans": {"regular": "WorkSans.ttf", "bold": "WorkSans.ttf"},
    "nunito": {"regular": "Nunito.ttf", "bold": "Nunito.ttf"},
}
# Real default when the brand's typed font name doesn't match any bundled
# family (or fonts.heading/body was left blank) — Inter, a clean, neutral
# sans that's a reasonable default for any visual profile, not a random
# pick.
_DEFAULT_FONT_KEY = "inter"


def caption_font_family(font_name: Optional[str]) -> str:
    """The family name video captions should ask for: the brand's own font when it is one of the bundled ones, otherwise
    Inter (also bundled). The same bundled files are handed to the renderer, so the name always resolves."""
    key = (font_name or "").strip().lower()
    return (key if key in _GOOGLE_FONTS else _DEFAULT_FONT_KEY).title()


def fonts_dir() -> str:
    return _FONTS_DIR


def _resolve_font_path(font_name: Optional[str], *, bold: bool) -> str:
    key = (font_name or "").strip().lower()
    entry = _GOOGLE_FONTS.get(key) or _GOOGLE_FONTS[_DEFAULT_FONT_KEY]
    filename = entry["bold" if bold else "regular"]
    return os.path.join(_FONTS_DIR, filename)


# Scripts the bundled brand fonts (Latin only) cannot draw. When the text contains one, the matching Noto Sans file is used for
# the whole text; it also carries the Latin letters, so a Tamil or Hindi line with English words in it draws all of them.
_SCRIPT_FONTS = (
    ((0x0B80, 0x0BFF), {"regular": "NotoSansTamil-Regular.ttf", "bold": "NotoSansTamil-Bold.ttf"}),          # Tamil
    ((0x0900, 0x097F), {"regular": "NotoSansDevanagari-Regular.ttf", "bold": "NotoSansDevanagari-Bold.ttf"}),  # Devanagari (Hindi)
)


def script_font_entry(text: Optional[str]) -> Optional[dict]:
    """The Noto font files for the first script in `text` that the brand fonts cannot draw, or None for ordinary Latin text."""
    for ch in text or "":
        code = ord(ch)
        for (low, high), entry in _SCRIPT_FONTS:
            if low <= code <= high:
                return entry
    return None


def shaping_available() -> bool:
    """Whether Pillow can shape complex scripts (Tamil and Hindi reorder vowel signs and join letters). Without the shaping
    engine the letters are real but a few combinations are drawn in the wrong order. Pillow's Linux builds normally include it;
    some others (for example the Windows ones) do not."""
    from PIL import features

    return bool(features.check("raqm"))


def _load_font(font_name: Optional[str], size: int, *, bold: bool = False, text: Optional[str] = None) -> ImageFont.FreeTypeFont:
    entry = script_font_entry(text)
    path = os.path.join(_FONTS_DIR, entry["bold" if bold else "regular"]) if entry else _resolve_font_path(font_name, bold=bold)
    try:
        return ImageFont.truetype(path, size=size)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Font load failed for %s, falling back to Pillow default: %s", path, exc)
        return ImageFont.load_default(size=size)


# Real pixel targets, confirmed 2026-09-26 directly against the shipped
# frontend (Frontend/Recast/app/(dashboard)/dashboard/pipelines/image/page.tsx
# lines 464-479) — the plan doc gave only one example (carousel_4_5); the
# other three came from the actual UI's own "sub" labels, not invented here.
LAYOUT_DIMS: dict[LayoutPreset, tuple[int, int]] = {
    LayoutPreset.QUOTE_1_1: (1200, 1200),
    LayoutPreset.CAROUSEL_4_5: (1080, 1350),
    LayoutPreset.STORY_9_16: (1080, 1920),
    LayoutPreset.HERO_16_9: (1920, 1080),
    # Apple Podcasts' / Spotify's real minimum cover-art requirement — see
    # pow/audio_image_pipeline/06-full-workflow-and-localization.md step 6.
    LayoutPreset.PODCAST_COVER: (3000, 3000),
    # bento/infographic/chart/code_snippet had NO declared pixel target
    # anywhere in the UI — a real product decision, made explicitly by the
    # user 2026-09-26 (bugs/gaps sweep, part 2) rather than guessed:
    # square modular grid, tall single-column infographic, a 4:3 chart
    # canvas, and a slightly-wider-than-tall code card, respectively. See
    # PROGRESS.md's Decisions Log for the exact rationale.
    LayoutPreset.BENTO: (1080, 1080),
    LayoutPreset.INFOGRAPHIC: (1080, 1920),
    LayoutPreset.CHART: (1200, 900),
    LayoutPreset.CODE_SNIPPET: (1200, 800),
    # Platform sizes (the platforms' commonly published recommendations; recheck each
    # platform's help page before release, they change).
    LayoutPreset.INSTAGRAM_SQUARE: (1080, 1080),
    LayoutPreset.LINKEDIN_POST: (1200, 627),
    LayoutPreset.X_POST: (1600, 900),
    LayoutPreset.SOCIAL_SHARE: (1200, 630),
    LayoutPreset.YOUTUBE_THUMBNAIL: (1280, 720),
}

# All 9 layouts now have a real pixel target (widened 2026-09-26, bugs/gaps
# sweep part 2, once the user made the real decision LAYOUT_DIMS above
# needed). Public (not underscore-prefixed) so app.api.v1.image_assets
# imports this exact set instead of maintaining a second copy that could
# drift from it. Multi-slide/carousel *management* (reorder, per-slide
# CRUD) is a separate, still-unbuilt concern — this set is only about
# render support for one slide at a time.
SUPPORTED_LAYOUTS = frozenset(LayoutPreset)

_DEFAULT_BG = "#1C1814"
_DEFAULT_FG = "#FFFFFF"
_MARGIN_FRACTION = 0.11  # matches render_quote_card's 120/1080 margin ratio


class BrandTokens(BaseModel):
    """Mirrors the frontend's real Zone-1 state (page.tsx lines 74-78) —
    field names intentionally match primaryHex/secondaryHex/accentHex/
    outerRadius/containerPadding exactly, not renamed."""

    primary_hex: str = "#e04a1f"
    secondary_hex: str = "#0f172a"
    accent_hex: str = "#38bdf8"
    outer_radius: int = 16
    container_padding: int = 24
    # Real brand typography, sourced from VisualIdentity.fonts.heading/.body
    # (free-text on the brand) — resolved against the bundled Google Fonts
    # set by _load_font, not rendered literally as arbitrary font names.
    heading_font: Optional[str] = None
    body_font: Optional[str] = None


class SlideTextContent(BaseModel):
    """headline/accent_keyword follow the plan's designed SlideTextContent
    shape (01-image-pipeline-plan.md's Slide.text_content). accent_keyword
    matches the frontend's real, already-editable `accentKeyword` state
    (page.tsx line 109) — headline/author/sticky_note_text do NOT exist as
    real per-slide state in the current mock UI yet (they're hardcoded
    display strings in the JSX, e.g. "...scale steadily while linear
    to-do lists choke under pressure" at page.tsx line 669); Stage 5 has to
    add real state for them, not just repoint an existing field."""

    headline: str
    accent_keyword: str = ""
    author: Optional[str] = None
    # Whether the brand logo and mascot were on this picture when it was first made, so "start again" in the editor and slides
    # added later keep the same brand treatment. None on pictures made before these were kept.
    show_logo: Optional[bool] = None
    show_mascot: Optional[bool] = None
    # A Lucide icon name (see pipelines/media/icons.py) drawn above the headline
    # in the accent colour. None draws no icon.
    icon_name: Optional[str] = None
    # A large, faint copy of the icon (or a default one) behind the text.
    illustration_accent: bool = False
    # False draws the picture with no headline, no icon and no band over it.
    show_text: bool = True


def _fit_headline(draw, headline: str, font_name: Optional[str], max_width: int, target_size: tuple[int, int]):
    """The biggest size at which the headline fits in at most 3 lines and about a quarter of the picture, shrinking before
    it ever cuts anything. If it still does not fit at the smallest size, whole words are dropped from the end and an
    ellipsis is added, so a word is never cut in half. Returns (font, lines, font_size)."""
    biggest = max(28, target_size[0] // 24)
    smallest = max(20, target_size[0] // 40)
    max_block = int(target_size[1] * 0.26)
    size = biggest
    while True:
        font = _load_font(font_name, size, bold=True, text=headline)
        lines = _wrap_text(draw, headline, font, max_width)
        if (len(lines) <= 3 and len(lines) * int(size * 1.3) <= max_block) or size <= smallest:
            break
        size -= 2
    if len(lines) > 3:
        words = headline.split()
        while words and len(lines) > 3:
            words.pop()
            lines = _wrap_text(draw, " ".join(words) + "…", font, max_width)
    return font, lines, size


def _brand_band_color(brand_tokens: "BrandTokens") -> tuple[int, int, int]:
    """A dark shade of the brand's own colour for the band behind the text, instead of flat black."""
    r, g, b = _rgb(_hex_or_default(brand_tokens.secondary_hex or brand_tokens.primary_hex, _DEFAULT_BG))
    return int(r * 0.35), int(g * 0.35), int(b * 0.35)


def _hex_or_default(value: str, fallback: str) -> str:
    value = (value or "").strip()
    if not value:
        return fallback
    try:
        Image.new("RGB", (1, 1), value)
        return value
    except ValueError:
        return fallback


def _rgb(color: str) -> tuple[int, int, int]:
    """(r, g, b) for any colour Pillow accepts ("#rrggbb", "red", "rgb(...)")."""
    r, g, b = ImageColor.getrgb(color)[:3]
    return r, g, b


def _wrap_text(draw: "ImageDraw.ImageDraw", text: str, font, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        box = draw.textbbox((0, 0), candidate, font=font)
        if box[2] - box[0] <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _fit_background(base_image_bytes: bytes, target_size: tuple[int, int]) -> Image.Image:
    """Cover-fit crop, the same behavior a CSS `background-size: cover`
    preview implies — fills target_size with no letterboxing, cropping
    the longer axis rather than distorting the aspect ratio."""
    img = Image.open(BytesIO(base_image_bytes)).convert("RGB")
    src_w, src_h = img.size
    target_w, target_h = target_size
    src_ratio = src_w / src_h
    target_ratio = target_w / target_h

    if src_ratio > target_ratio:
        # Source is wider than target — crop left/right.
        new_w = int(src_h * target_ratio)
        left = (src_w - new_w) // 2
        img = img.crop((left, 0, left + new_w, src_h))
    else:
        # Source is taller than target — crop top/bottom.
        new_h = int(src_w / target_ratio)
        top = (src_h - new_h) // 2
        img = img.crop((0, top, src_w, top + new_h))

    scale = target_w / img.width
    img = img.resize(target_size, Image.LANCZOS)
    if scale > 1.08:
        # the free picture models make about 1024 pixels, so most layouts enlarge them; a light unsharp mask gives back some of
        # the crispness the enlargement softens (it is skipped when the picture is not enlarged)
        img = img.filter(ImageFilter.UnsharpMask(radius=1.4, percent=70, threshold=3))
    return img


def render_slide(
    *,
    layout: LayoutPreset,
    base_image_bytes: Optional[bytes],
    brand_tokens: BrandTokens,
    text_content: SlideTextContent,
    logo_bytes: Optional[bytes] = None,
    mascot_bytes: Optional[bytes] = None,
) -> bytes:
    """Composites one real PNG for any of the 9 layouts (see module
    docstring) at its real pixel target. `LAYOUT_DIMS` is the true source
    of truth; the explicit check below is a defensive guard against a
    future `LayoutPreset` value being added without a matching dimension
    entry, not a currently-reachable path today.
    """
    if layout not in LAYOUT_DIMS:
        raise ValueError(
            f"render_slide has no declared pixel target for {layout.value} — "
            "add it to LAYOUT_DIMS (a real product decision, not a guess) "
            "before this layout can render."
        )

    target_size = LAYOUT_DIMS[layout]
    primary = _hex_or_default(brand_tokens.primary_hex, _DEFAULT_BG)
    accent = _hex_or_default(brand_tokens.accent_hex, _DEFAULT_FG)

    if base_image_bytes:
        try:
            img = _fit_background(base_image_bytes, target_size)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Background image decode failed, using solid brand color: %s", exc)
            img = Image.new("RGB", target_size, primary)
    else:
        img = Image.new("RGB", target_size, primary)

    # A real, semi-opaque scrim behind the text so headline copy stays
    # legible over a photographic background — not a decorative effect,
    # a legibility requirement for any layout that can have a real photo
    # background (Stage 2's effects-deferral is about drop-shadow/dual-
    # tone/grid-texture toggles, not "no way to read the text at all").
    overlay = Image.new("RGBA", target_size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    margin = int(min(target_size) * _MARGIN_FRACTION)
    max_text_width = target_size[0] - margin * 2
    show_text = text_content.show_text
    headline_font_size = max(24, target_size[0] // 24)
    font = None
    lines: list[str] = []
    if show_text:
        headline = text_content.headline.strip()
        if headline:
            font, lines, headline_font_size = _fit_headline(draw, headline, brand_tokens.heading_font, max_text_width, target_size)
        else:
            show_text = False  # nothing to say means no band and no words, never a placeholder
    line_height = int(headline_font_size * 1.3)
    text_block_height = len(lines) * line_height
    # An icon sits just above the headline, inside the band so it stays legible.
    icon_name = text_content.icon_name if show_text and is_known_icon(text_content.icon_name) else None
    icon_size = int(min(target_size) * 0.12) if icon_name else 0
    icon_gap = margin // 3 if icon_name else 0
    bar_height = max(4, target_size[1] // 200) if show_text and not icon_name else 0
    bar_gap = margin // 3 if bar_height else 0

    if show_text:
        # A soft band in a dark shade of the brand colour, fading in from the picture, so the words are readable
        # without a hard black block across the picture.
        block = text_block_height + icon_size + icon_gap + bar_height + bar_gap
        band_top = max(0, target_size[1] - margin - block - margin // 2)
        fade = margin * 2
        r, g, b = _brand_band_color(brand_tokens)
        total = target_size[1] - max(0, band_top - fade)
        for y in range(max(0, band_top - fade), target_size[1]):
            progress = (y - max(0, band_top - fade)) / max(1, total)
            alpha = int(215 * min(1.0, progress * 1.6) ** 1.2)
            draw.line([(0, y), (target_size[0], y)], fill=(r, g, b, alpha))

    # Illustration accent: a big, faint glyph in the corner, drawn after the dark strip behind the text, so it runs through it without a hard edge.
    if text_content.illustration_accent:
        accent_icon = icon_name or DEFAULT_ACCENT_ICON
        # Big enough to read as an illustration, placed so most of the icon is
        # visible with only its far edge running off the corner.
        art_size = int(min(target_size) * 0.6)
        art_font = load_icon_font(art_size)
        r, g, b = _rgb(accent)
        art_x = target_size[0] - int(art_size * 0.92)
        art_y = int(margin * 0.4)
        draw.text((art_x, art_y), icon_char(accent_icon), font=art_font, fill=(r, g, b, 46))

    img = img.convert("RGBA")
    img = Image.alpha_composite(img, overlay)

    # Real logo compositing (G-3, resolved 2026-09-26) — a small watermark
    # in the top-left corner, matching the frontend mock's own "Top Meta
    # Bar" badge position. Real brand_tokens.logo_bytes only, never a
    # placeholder shape — no logo set means no badge drawn, same as the
    # scrim/headline logic above never inventing content that isn't real.
    if logo_bytes:
        try:
            logo = Image.open(BytesIO(logo_bytes)).convert("RGBA")
            logo_target = int(min(target_size) * 0.08)
            logo.thumbnail((logo_target, logo_target), Image.LANCZOS)
            img.paste(logo, (margin, margin), logo)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Logo composite failed, skipping: %s", exc)

    if mascot_bytes:
        try:
            mascot = Image.open(BytesIO(mascot_bytes)).convert("RGBA")
            mascot_target = int(min(target_size) * 0.2)
            mascot.thumbnail((mascot_target, mascot_target), Image.LANCZOS)
            img.paste(mascot, (target_size[0] - mascot.width - margin // 2, margin // 2), mascot)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Mascot composite failed, skipping: %s", exc)

    draw = ImageDraw.Draw(img)

    if bar_height:
        bar_top = target_size[1] - margin - text_block_height - bar_gap - bar_height
        draw.rectangle([(margin, bar_top), (margin + int(target_size[0] * 0.09), bar_top + bar_height)], fill=accent)

    if icon_name:
        icon_font = load_icon_font(icon_size)
        draw.text(
            (margin, target_size[1] - margin - text_block_height - icon_gap - icon_size),
            icon_char(icon_name),
            font=icon_font,
            fill=accent,
        )

    accent_keyword = (text_content.accent_keyword or "").strip()
    y = target_size[1] - margin - text_block_height
    for line in lines:
        x = margin
        if accent_keyword and accent_keyword.lower() in line.lower():
            # Real substring highlight, matching the frontend preview's own
            # treatment of accentKeyword (page.tsx lines 665-670) — not the
            # whole line recolored, just the matched keyword span.
            idx = line.lower().index(accent_keyword.lower())
            before, matched, after = (
                line[:idx],
                line[idx : idx + len(accent_keyword)],
                line[idx + len(accent_keyword) :],
            )
            draw.text((x, y), before, font=font, fill=_DEFAULT_FG)
            x += draw.textbbox((0, 0), before, font=font)[2]
            draw.text((x, y), matched, font=font, fill=accent)
            x += draw.textbbox((0, 0), matched, font=font)[2]
            draw.text((x, y), after, font=font, fill=_DEFAULT_FG)
        else:
            draw.text((x, y), line, font=font, fill=_DEFAULT_FG)
        y += line_height

    if text_content.author:
        author_font = _load_font(brand_tokens.body_font, max(16, headline_font_size // 2), bold=False)
        draw.text((margin, margin // 2), text_content.author.strip(), font=author_font, fill=_DEFAULT_FG)

    buf = BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()
