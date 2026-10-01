"""Truthful text and proper pictures: prompts, claims check on every text flow, headline, picture overlay, brand assets.
No network and no real keys."""
import asyncio
import io
from pathlib import Path

from PIL import Image

from app.models.image_asset import LayoutPreset
from app.pipelines.media import headline as hl
from app.pipelines.media.image_render import BrandTokens, SlideTextContent, render_slide
from app.prompts.registry import load_prompt

PROMPTS = Path(__file__).resolve().parents[1] / "app" / "prompts"


def _background() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (800, 800), (90, 120, 160)).save(buf, "JPEG")
    return buf.getvalue()


# ---- prompts: nothing asks the model to invent numbers, dates or events -------------------------------------------------------------
def test_no_platform_rule_or_chip_demands_numbers_or_made_up_stories():
    rules = {p.name: p.read_text(encoding="utf-8") for p in (PROMPTS / "text/generate/platform_rules").glob("*.jinja")}
    assert "at least 2 specific numbers" not in rules["linkedin.jinja"]
    for name in ("linkedin.jinja", "facebook.jinja", "instagram.jinja", "newsletter.jinja"):
        assert "Never invent figures" in rules[name], name
    chips = PROMPTS / "text/refine/chips"
    for name in ("add_numbers", "add_story", "expand"):
        text = (chips / f"{name}.jinja").read_text(encoding="utf-8")
        assert "invent" in text.lower() or "not already in the content" in text, name
    assert "Do not invent a named person" in (chips / "add_story.jinja").read_text(encoding="utf-8")


def test_the_research_brief_cannot_supply_figures_and_the_specificity_rule_forbids_made_up_dates():
    brief = load_prompt("text/normalize/research_topic", topic="tone consistency", language="en")
    assert "Relevant statistics" not in brief and "anything invented here would be published as fact" in brief
    spec = (PROMPTS / "text/generate/specificity.jinja").read_text(encoding="utf-8")
    assert 'say "last Tuesday"' not in spec and "NO INVENTED FACTS" in spec


# ---- headline ---------------------------------------------------------------------------------------------------------------------------------
def test_a_headline_is_never_cut_inside_a_word():
    text = "Your current posting process is silently draining budget and brand voice, and no one's calling it out."
    out = hl.trim_headline(text)
    assert out.endswith("…") and len(out.split()) <= hl.MAX_WORDS and len(out) <= hl.MAX_CHARS + 1
    assert all(word in text for word in out.rstrip("…").split())
    assert hl.trim_headline("Short one.") == "Short one"
    assert hl.trim_headline("") == ""


def test_make_headline_uses_the_model_line_when_usable_and_falls_back_otherwise(monkeypatch):
    import app.shared.llm as llm

    post = "Manual tone tweaks cost hours every week. Recast keeps your voice steady across languages."

    async def good(*a, **k):
        return "Tone tweaks cost hours"

    monkeypatch.setattr(llm, "call_llm", good)
    assert asyncio.run(hl.make_headline(post)) == "Tone tweaks cost hours"

    async def invents(*a, **k):
        return "98% faster brand voice"

    monkeypatch.setattr(llm, "call_llm", invents)
    assert asyncio.run(hl.make_headline(post)) == "Manual tone tweaks cost hours every week"

    async def fails(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(llm, "call_llm", fails)
    assert asyncio.run(hl.make_headline(post)) == "Manual tone tweaks cost hours every week"


# ---- picture overlay --------------------------------------------------------------------------------------------------------------------------
def test_a_long_headline_fits_without_losing_words_and_text_can_be_turned_off():
    tokens = BrandTokens(primary_hex="#6366f1", secondary_hex="#0f172a", accent_hex="#f59e0b", heading_font="Poppins")
    long = "Your current posting process is silently draining budget and brand voice and no one is calling it out loud enough"
    plain = Image.open(io.BytesIO(render_slide(layout=LayoutPreset.QUOTE_1_1, base_image_bytes=_background(), brand_tokens=tokens,
                                              text_content=SlideTextContent(headline=long, show_text=False)))).convert("RGB")
    with_text = Image.open(io.BytesIO(render_slide(layout=LayoutPreset.QUOTE_1_1, base_image_bytes=_background(), brand_tokens=tokens,
                                                  text_content=SlideTextContent(headline=long)))).convert("RGB")
    w, h = plain.size
    # no band when text is off: the bottom row is the plain picture; with text it is darkened in the brand shade
    assert sum(plain.getpixel((5, h - 5))) > sum(with_text.getpixel((5, h - 5))) + 120
    # the top of the picture is untouched either way
    assert plain.getpixel((w // 2, 10)) == with_text.getpixel((w // 2, 10))
    # the accent bar colour appears in the text version only
    amber = (245, 158, 11)
    assert any(with_text.getpixel((x, y)) == amber for x in range(100, 300, 5) for y in range(h // 2, h, 2))
    assert not any(plain.getpixel((x, y)) == amber for x in range(100, 300, 5) for y in range(h // 2, h, 2))


def test_a_mascot_is_added_only_when_given():
    tokens = BrandTokens(primary_hex="#6366f1", secondary_hex="#0f172a", accent_hex="#f59e0b")
    mascot = io.BytesIO()
    Image.new("RGBA", (200, 200), (255, 0, 0, 255)).save(mascot, "PNG")
    kw = dict(layout=LayoutPreset.QUOTE_1_1, base_image_bytes=_background(), brand_tokens=tokens, text_content=SlideTextContent(headline="Hi there"))
    without = Image.open(io.BytesIO(render_slide(**kw))).convert("RGB")
    with_mascot = Image.open(io.BytesIO(render_slide(**kw, mascot_bytes=mascot.getvalue()))).convert("RGB")
    corner = (without.width - 120, 80)
    assert with_mascot.getpixel(corner) == (255, 0, 0) and without.getpixel(corner) != (255, 0, 0)


# ---- campaign picture request: brand assets on by default, text option ------------------------------------------------------------------
def test_campaign_pictures_default_to_logo_on_and_a_short_headline(monkeypatch):
    from app.pipelines.campaigns import media

    async def fake_headline(content, language_name="English"):
        return "Tone tweaks cost hours"

    monkeypatch.setattr("app.pipelines.media.headline.make_headline", fake_headline)
    campaign = {"brand_id": "b1", "media_plan": {"count_per_post": 1, "image": {"layout": "quote_1_1"}}}
    piece = {"piece_id": "p1", "content": "A long first sentence that used to be pasted onto the picture. And more."}
    req = asyncio.run(media._image_request(campaign, piece))
    assert req.headline == "Tone tweaks cost hours" and req.show_text is True and req.show_logo is True and req.show_mascot is False

    campaign["media_plan"]["image"].update({"text": "none", "mascot": True, "logo": False})
    req = asyncio.run(media._image_request(campaign, piece))
    assert req.headline == "" and req.show_text is False and req.show_logo is False and req.show_mascot is True


def test_the_picture_prompt_is_built_from_the_post_and_steers_away_from_faces():
    from app.api.v1.image_assets import _piece_topic
    from app.pipelines.media import image_generation as ig

    piece = {"content": "First line.\n\nSecond paragraph about tone drift across languages.\n" + "x" * 900}
    topic = _piece_topic(piece)
    assert "Second paragraph" in topic and len(topic) <= 600
    raw = ig._build_raw_prompt(topic, {"visual_identity": {"colors": {"primary": "#112233"}, "visual_style_notes": "calm, editorial"}})
    assert "visual metaphor" in raw and "dominant palette" in raw and "#112233" in raw
    import inspect

    assert "avoid close-up faces" in inspect.getsource(ig._polish_prompt)


# ---- claims check on every text flow ----------------------------------------------------------------------------------------------------
def test_quick_edits_report_new_unbacked_claims(monkeypatch):
    from app.pipelines.text import chips

    async def fake_llm(prompt, **k):
        return "Recast saves 12 minutes per post and scored 96% on keyword compliance."

    monkeypatch.setattr(chips, "call_llm", fake_llm)
    out = asyncio.run(chips.apply_chip(content="Recast saves time on every post.", chip_name="add_numbers", platform="LinkedIn", brand_context="Recast: brand voice tool."))
    assert out["changed"] and any("96%" in w for w in out["claim_warnings"]) and any("12 minutes" in w for w in out["claim_warnings"])


def test_a_longer_script_that_adds_a_number_is_flagged(monkeypatch):
    from app.pipelines.media import fit_script

    async def llm(prompt):
        return "Recast keeps your tone steady. Teams save 40 hours a month. " * 6

    original = "Recast keeps your tone steady across languages for every team."
    out = asyncio.run(fit_script.fit_script(original, 90, 150.0, "English", llm))
    if out["status"] == "fitted":
        assert any("40 hours" in w for w in out.get("claim_warnings", []))


def test_the_campaign_plan_keeps_the_picture_options_and_defaults_to_brand_assets_on():
    from app.models.campaign import CampaignMediaPlan

    default = CampaignMediaPlan().image
    assert default.text == "headline" and default.logo is True and default.mascot is False
    custom = CampaignMediaPlan(enabled=True, kinds=["image"], image={"layout": "quote_1_1", "text": "none", "mascot": True, "logo": False}).image
    assert (custom.text, custom.logo, custom.mascot) == ("none", False, True)


def test_nothing_prints_a_placeholder_on_a_picture_and_a_brandless_card_still_renders():
    from app.pipelines.media.default_image import render_quote_card

    tokens = BrandTokens(primary_hex="", secondary_hex="", accent_hex="")
    out = Image.open(io.BytesIO(render_slide(layout=LayoutPreset.QUOTE_1_1, base_image_bytes=None, brand_tokens=tokens,
                                             text_content=SlideTextContent(headline="")))).convert("RGB")
    # no headline: a plain brand colour card, no band or words (every pixel in the bottom half matches the top)
    assert out.getpixel((300, out.height - 100)) == out.getpixel((300, 100))
    assert len(render_quote_card("", {})) > 1000 and len(render_quote_card("Plain words on a card", {"colors": {"primary": "#222222", "accent": "#262626"}})) > 1000
    assert "Recast" not in Path(__file__).resolve().parents[1].joinpath("app/pipelines/media/image_render.py").read_text(encoding="utf-8").split("def render_slide")[1]


def test_the_campaign_plan_keeps_the_picture_options_and_defaults_to_brand_assets_on():
    from app.models.campaign import CampaignMediaPlan

    default = CampaignMediaPlan().image
    assert default.text == "headline" and default.logo is True and default.mascot is False
    custom = CampaignMediaPlan(enabled=True, kinds=["image"], image={"layout": "quote_1_1", "text": "none", "mascot": True, "logo": False}).image
    assert (custom.text, custom.logo, custom.mascot) == ("none", False, True)


def test_nothing_prints_a_placeholder_on_a_picture_and_a_brandless_card_still_renders():
    from app.pipelines.media.default_image import render_quote_card

    tokens = BrandTokens(primary_hex="", secondary_hex="", accent_hex="")
    out = Image.open(io.BytesIO(render_slide(layout=LayoutPreset.QUOTE_1_1, base_image_bytes=None, brand_tokens=tokens,
                                             text_content=SlideTextContent(headline="")))).convert("RGB")
    # no headline: a plain card with no band or words, so the bottom matches the top
    assert out.getpixel((300, out.height - 100)) == out.getpixel((300, 100))
    assert len(render_quote_card("", {})) > 1000
    assert len(render_quote_card("Plain words on a card", {"colors": {"primary": "#222222", "accent": "#262626"}})) > 1000
    source = (Path(__file__).resolve().parents[1] / "app/pipelines/media/image_render.py").read_text(encoding="utf-8")
    assert '"Recast"' not in source.split("def render_slide")[1]


def test_a_screen_that_should_show_a_dashboard_is_made_soft_so_no_fake_interface_text_is_drawn():
    from app.pipelines.media.image_generation import SCREEN_SUFFIX, enforce_no_text, soften_screens

    prompt = ("A sleek monitor shows a content dashboard with glowing modular cards connected by thin lines, representing a scalable system. "
              "A tidy desk holds a notebook and a lamp.")
    soft = soften_screens(prompt)
    assert "dashboard" not in soft and "modular cards" not in soft and "softly glowing, out-of-focus screen" in soft
    assert "tidy desk holds a notebook and a lamp" in soft and "representing a scalable system" in soft
    assert SCREEN_SUFFIX.strip() in enforce_no_text(prompt)
    assert soften_screens("A sunrise over calm hills.") == "A sunrise over calm hills."
    assert SCREEN_SUFFIX.strip() not in enforce_no_text("A sunrise over calm hills.")


def test_the_members_avoid_list_reaches_the_prompt_writer_and_an_enlarged_picture_is_sharpened():
    import asyncio

    from app.pipelines.media import image_generation as ig
    from app.pipelines.media.image_render import _fit_background

    assert "faces, hands" in ig._avoid_clause("faces, hands") and ig._avoid_clause("  ") == ""
    seen = {}

    async def fake_polish(raw):
        seen["raw"] = raw
        return "a calm desk"

    async def safe(_):
        return "safe"

    async def fits(*_):
        return True

    import pytest

    mp = pytest.MonkeyPatch()
    mp.setattr(ig, "_polish_prompt", fake_polish)
    mp.setattr(ig, "_safety_verdict", safe)
    mp.setattr(ig, "_brand_fit_gate", fits)
    try:
        out = asyncio.run(ig._run_gates("a desk", {}, "stock people, clutter"))
    finally:
        mp.undo()
    assert "stock people, clutter" in seen["raw"] and out and out.startswith("a calm desk")

    # an enlarged picture is lightly sharpened; one that is not enlarged is left alone
    import io

    from PIL import Image

    checker = Image.new("RGB", (256, 256), (0, 0, 0))
    for x in range(0, 256, 2):
        for y in range(256):
            checker.putpixel((x, y), (255, 255, 255))
    buf = io.BytesIO()
    checker.save(buf, "PNG")
    enlarged = _fit_background(buf.getvalue(), (512, 512))
    plain = Image.open(io.BytesIO(buf.getvalue())).convert("RGB").resize((512, 512), Image.LANCZOS)
    assert enlarged.tobytes() != plain.tobytes()
    same = _fit_background(buf.getvalue(), (256, 256))
    assert same.tobytes() == Image.open(io.BytesIO(buf.getvalue())).convert("RGB").tobytes()
