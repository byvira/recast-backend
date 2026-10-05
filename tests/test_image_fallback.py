"""When the AI picture cannot be made the slide is a text card. These tests check that the reason is kept,
that only the safety check can stop a picture, and that a card without a picture says so. No network."""
import asyncio

from app.pipelines.media import image_generation as ig


def run(coro):
    return asyncio.run(coro)


def with_reason(coro_fn):
    """Runs the call and reads the reason in the same task, as real callers do."""
    async def go():
        out = await coro_fn()
        return out, ig.last_failure_reason()
    return asyncio.run(go())


def _patch_polish(monkeypatch, verdict="safe", generic_ok=True, fit_ok=True):
    async def polish(raw):
        return "A calm desk with soft morning light"

    async def safety(prompt):
        return verdict

    async def fit(prompt, brand):
        return fit_ok

    monkeypatch.setattr(ig, "_polish_prompt", polish)
    monkeypatch.setattr(ig, "_safety_verdict", safety)
    monkeypatch.setattr(ig, "_anti_generic_gate", lambda p: generic_ok)
    monkeypatch.setattr(ig, "_brand_fit_gate", fit)


def test_a_safe_prompt_is_used_even_when_both_style_gates_object(monkeypatch):
    _patch_polish(monkeypatch, verdict="safe", generic_ok=False, fit_ok=False)
    out = run(ig._run_gates("topic", {}))
    assert out and out.endswith(ig.NO_TEXT_SUFFIX.strip())


def test_a_prompt_that_fails_the_safety_check_gets_no_picture_and_a_reason(monkeypatch):
    _patch_polish(monkeypatch, verdict="unsafe")
    out, reason = with_reason(lambda: ig._run_gates("topic", {}))
    assert out is None
    assert "safety check" in (reason or "")


def test_a_safety_check_that_could_not_run_says_so(monkeypatch):
    _patch_polish(monkeypatch, verdict="error")
    out, reason = with_reason(lambda: ig._run_gates("topic", {}))
    assert out is None
    assert "could not run" in (reason or "")


def test_no_provider_is_reported(monkeypatch):
    monkeypatch.setattr(ig.settings, "CLOUDFLARE_API_TOKEN", "", raising=False)
    monkeypatch.setattr(ig.settings, "GEMINI_API_KEY", "", raising=False)
    monkeypatch.setattr(ig.settings, "HUGGINGFACE_API_TOKEN", "", raising=False)
    monkeypatch.setattr(ig.settings, "POLLINATIONS_ENABLED", False, raising=False)
    out, reason = with_reason(lambda: ig.generate_image_from_prompt(prompt="x", workspace_id="w", user_id="u", target_size=(1024, 1024)))
    assert out is None
    assert reason == "No image service is set up."


def test_a_provider_failure_is_reported(monkeypatch):
    async def boom(prompt):
        raise RuntimeError("429")

    monkeypatch.setattr(ig, "_call_cloudflare", boom)
    monkeypatch.setattr(ig.settings, "CLOUDFLARE_API_TOKEN", "t", raising=False)
    monkeypatch.setattr(ig.settings, "CLOUDFLARE_ACCOUNT_ID", "a", raising=False)
    monkeypatch.setattr(ig.settings, "GEMINI_API_KEY", "", raising=False)
    monkeypatch.setattr(ig.settings, "HUGGINGFACE_API_TOKEN", "", raising=False)
    monkeypatch.setattr(ig.settings, "POLLINATIONS_ENABLED", False, raising=False)
    out, reason = with_reason(lambda: ig._generate_image_bytes("p"))
    assert out is None
    assert "Cloudflare did not return a picture" in (reason or "")


def test_a_missing_cloudflare_token_still_reaches_the_backups(monkeypatch):
    async def never(prompt):
        raise AssertionError("Cloudflare must not be called without a token")

    async def gemini(prompt):
        return b"gemini-picture"

    async def slot():
        return True

    monkeypatch.setattr(ig, "_call_cloudflare", never)
    monkeypatch.setattr(ig, "_call_gemini", gemini)
    monkeypatch.setattr(ig, "_gemini_fallback_slot_available", slot)
    monkeypatch.setattr(ig, "_gemini_skip_until", 0.0)
    monkeypatch.setattr(ig.settings, "CLOUDFLARE_API_TOKEN", "", raising=False)
    monkeypatch.setattr(ig.settings, "GEMINI_API_KEY", "k", raising=False)
    out, _ = with_reason(lambda: ig._generate_image_bytes("p"))
    assert out == b"gemini-picture"


def test_open_backups_are_tried_when_cloudflare_and_gemini_are_both_unusable(monkeypatch):
    from app.shared import open_fallbacks

    async def open_pic(prompt, errors=None):
        return b"open-picture"

    monkeypatch.setattr(open_fallbacks, "open_image_fallback", open_pic)
    monkeypatch.setattr(ig.settings, "CLOUDFLARE_API_TOKEN", "", raising=False)
    monkeypatch.setattr(ig.settings, "GEMINI_API_KEY", "", raising=False)
    monkeypatch.setattr(ig.settings, "POLLINATIONS_ENABLED", True, raising=False)

    out, _ = with_reason(lambda: ig._generate_image_bytes("p"))
    assert out == b"open-picture"


def test_a_failed_gemini_call_gives_its_slot_back_and_a_daily_quota_pauses_it(monkeypatch):
    refunded = []

    async def gemini(prompt):
        raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded for metric PerDay limit: 0")

    async def slot():
        return True

    async def refund():
        refunded.append(1)

    monkeypatch.setattr(ig, "_call_gemini", gemini)
    monkeypatch.setattr(ig, "_gemini_fallback_slot_available", slot)
    monkeypatch.setattr(ig, "_refund_gemini_slot", refund)
    monkeypatch.setattr(ig, "_gemini_skip_until", 0.0)
    monkeypatch.setattr(ig.settings, "CLOUDFLARE_API_TOKEN", "", raising=False)
    monkeypatch.setattr(ig.settings, "GEMINI_API_KEY", "k", raising=False)
    monkeypatch.setattr(ig.settings, "HUGGINGFACE_API_TOKEN", "", raising=False)
    monkeypatch.setattr(ig.settings, "POLLINATIONS_ENABLED", False, raising=False)
    out, reason = with_reason(lambda: ig._generate_image_bytes("p"))
    assert out is None and refunded == [1]
    assert "Gemini did not return a picture" in (reason or "")
    assert ig._gemini_skip_until > 0.0


def test_a_vision_check_that_returned_nothing_does_not_flag_the_picture(monkeypatch):
    async def blank(*a, **k):
        return ""

    monkeypatch.setattr(ig, "call_vision", blank)
    flagged, reason = run(ig._qa_gate(b"x", {"visual_identity": {"visual_style_notes": "calm and minimal"}}))
    assert flagged is False and reason is None


def test_the_reason_is_cleared_at_the_start_of_each_call(monkeypatch):
    monkeypatch.setattr(ig.settings, "CLOUDFLARE_API_TOKEN", "", raising=False)
    monkeypatch.setattr(ig.settings, "GEMINI_API_KEY", "", raising=False)

    async def go():
        ig._last_failure.set("an old reason")
        await ig.generate_image_from_prompt(prompt="x", workspace_id="w", user_id="u", target_size=(1, 1))
        return ig.last_failure_reason()

    assert asyncio.run(go()) == "No image service is set up."


def test_a_card_without_a_picture_is_flagged():
    from app.api.v1 import image_assets as ia

    ig._last_failure.set("The image service did not return a picture.")
    note = ia._fallback_note(None)
    assert note and note.startswith("No AI picture, so this is a text card.") and "did not return a picture" in note
    assert ia._fallback_note(b"png") is None
