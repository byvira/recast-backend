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
    out, reason = with_reason(lambda: ig.generate_image_from_prompt(prompt="x", workspace_id="w", user_id="u", target_size=(1024, 1024)))
    assert out is None
    assert reason == "No image service is set up."


def test_a_provider_failure_is_reported(monkeypatch):
    async def boom(prompt):
        raise RuntimeError("429")

    monkeypatch.setattr(ig, "_call_cloudflare", boom)
    monkeypatch.setattr(ig.settings, "GEMINI_API_KEY", "", raising=False)
    out, reason = with_reason(lambda: ig._generate_image_bytes("p"))
    assert out is None
    assert "did not return a picture" in (reason or "")


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
