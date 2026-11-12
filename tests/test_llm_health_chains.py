"""The LLM health page cannot go stale: every provider the code calls is listed on the page, every chain step names a listed provider,
and the chain view reports a thin or broken chain. No network."""
import re
from pathlib import Path

from app.shared.llm_health import chains, health

APP = Path(__file__).resolve().parents[1] / "app"


def _called_providers() -> set[str]:
    """Provider names passed to the health recorder's track() or note_fallback_failed() anywhere in the app."""
    found: set[str] = set()
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "llm_health.track import" not in text and "llm_health import track" not in text:
            continue
        found.update(re.findall(r'\btrack\("([a-z_]+)"', text))
        found.update(re.findall(r'note_fallback_failed\("([a-z_]+)"', text))
    return found


def test_every_provider_the_code_calls_is_listed_on_the_page():
    missing = _called_providers() - set(health.PROVIDERS)
    assert not missing, f"Add these to health.PROVIDERS so the page shows them: {sorted(missing)}"


def test_every_chain_step_names_a_listed_provider_with_limits_and_a_reset_rule():
    for name in chains.providers_in_chains():
        assert name in health.PROVIDERS, name
        assert name in health.DEFAULT_LIMITS and name in health.DEFAULT_RESET, name


def test_every_listed_provider_is_used_by_a_chain_or_the_code():
    used = chains.providers_in_chains() | _called_providers()
    unused = set(health.PROVIDERS) - used
    assert not unused, f"Listed but never used: {sorted(unused)}"


def _states(**by_provider):
    return {name: {"configured": True, "label": name.capitalize(), "status": status} for name, status in by_provider.items()}


def test_a_chain_with_one_working_step_is_thin_and_with_none_is_broken():
    names = chains.providers_in_chains()
    healthy = {n: {"configured": True, "label": n, "status": "healthy"} for n in names}
    assert next(c for c in chains.describe(healthy) if c["id"] == "embeddings")["depth"] == "ok"

    def down(*providers):
        return {**healthy, **{p: {"configured": True, "label": p, "status": "down"} for p in providers}}

    # the embeddings chain is Gemini, Mistral, Cloudflare: take two away and one is left, take all away and none is
    thin = next(c for c in chains.describe(down("gemini", "mistral")) if c["id"] == "embeddings")
    assert thin["depth"] == "thin" and thin["usable"] == 1
    assert next(c for c in chains.describe(down("gemini", "mistral", "cloudflare")) if c["id"] == "embeddings")["depth"] == "broken"

    voice = next(c for c in chains.describe(healthy) if c["id"] == "voice")
    assert voice["depth"] == "ok" and [s["provider"] for s in voice["steps"]][-1] == "gemini"


def test_a_step_that_is_switched_off_or_has_no_key_is_not_counted():
    states = {n: {"configured": True, "label": n, "status": "healthy"} for n in chains.providers_in_chains()}
    states["deepgram"]["configured"] = False
    voice = next(c for c in chains.describe(states) if c["id"] == "voice")
    deepgram = next(s for s in voice["steps"] if s["provider"] == "deepgram")
    assert deepgram["status"] == "not_set" and deepgram["usable"] is False


# ── the scheduled model check ────────────────────────────────────────────────

from datetime import datetime, timedelta, timezone

import httpx

from app.shared.llm_health import probe


def _status_error(status: int, body: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.test")
    return httpx.HTTPStatusError("failed", request=request, response=httpx.Response(status, text=body, request=request))


def test_a_failed_check_is_read_in_plain_words_without_the_providers_text():
    assert probe.read_failure(_status_error(404, "no such model"))["state"] == "retired"
    assert probe.read_failure(_status_error(410, "The requested model is deprecated"))["state"] == "retired"
    assert probe.read_failure(_status_error(401))["state"] == "down"
    assert probe.read_failure(_status_error(402, "payment_required"))["state"] == "down"
    assert probe.read_failure(_status_error(429, "quota"))["state"] == "limit"
    assert probe.read_failure(_status_error(503))["state"] == "down"
    assert probe.read_failure(_status_error(400, "Input prompt contains NSFW content"))["state"] == "unsure"
    assert probe.read_failure(httpx.ReadTimeout("slow"))["state"] == "down"
    assert "secret-token" not in probe.read_failure(_status_error(500, "secret-token"))["detail"]


def test_a_reading_the_provider_could_not_judge_changes_nothing_but_the_time():
    now = datetime.now(timezone.utc)
    ok = probe.settle(None, {"state": "ok", "detail": "Answered."}, now=now)
    again = probe.settle(ok, {"state": "unsure", "detail": "filter"}, now=now + timedelta(hours=6))
    assert again["state"] == "ok" and again["checked_at"] > ok["checked_at"]
    down = probe.settle(ok, {"state": "down", "detail": "x"}, now=now)
    assert down["fail_streak"] == 1 and probe.settle(down, {"state": "ok", "detail": "y"}, now=now)["fail_streak"] == 0


def test_a_down_reading_goes_stale_and_is_not_trusted_for_routing():
    now = datetime.now(timezone.utc)
    assert probe.step_state({"state": "retired", "checked_at": now}) == "down"
    assert probe.step_state({"state": "limit", "checked_at": now}) == "limit_reached"
    assert probe.step_state({"state": "down", "checked_at": now - timedelta(hours=20)}) is None
    assert probe.step_state({"state": "ok", "checked_at": now - timedelta(hours=20)}) == "healthy"
    assert probe.step_state(None) is None


def test_every_step_that_can_be_checked_has_a_check_and_nothing_checks_a_model_no_chain_uses():
    target_keys = {t.key for t in probe.targets()}
    chain_keys = {probe.key_for(cid, s.provider, m) for cid, c in chains.all_chains().items() for s in c["steps"] if s.on() for m in s.models}
    # every target belongs to a chain step, so the page never hides a model the check is spending calls on
    assert target_keys <= chain_keys, sorted(target_keys - chain_keys)
    # a step that is on and has a key but no check would be invisible to the check: only the paid picture step and Hugging Face may lack one
    unchecked = {(cid, s.provider) for cid, c in chains.all_chains().items() for s in c["steps"] if s.on() and not s.models}
    assert unchecked <= {("pictures", "gemini"), ("pictures", "huggingface")}, unchecked


def test_the_chain_view_uses_the_check_and_a_step_with_several_models_is_down_only_when_all_are():
    now = datetime.now(timezone.utc)
    states = {n: {"configured": True, "label": n, "status": "idle"} for n in chains.providers_in_chains()}
    first, second = (probe.key_for("writing", "openrouter", m) for m in (probe.settings.OPENROUTER_MODEL, probe.settings.OPENROUTER_MODEL_2))
    readings = {first: {"state": "retired", "detail": "gone", "checked_at": now}, second: {"state": "ok", "detail": "Answered.", "checked_at": now}}
    step = next(s for s in next(c for c in chains.describe(states, readings) if c["id"] == "writing")["steps"] if s["provider"] == "openrouter")
    assert step["usable"] and step["retired"] and step["detail"] == "gone" and step["status"] == "healthy"
    readings[second] = {"state": "down", "detail": "x", "checked_at": now}
    step = next(s for s in next(c for c in chains.describe(states, readings) if c["id"] == "writing")["steps"] if s["provider"] == "openrouter")
    assert step["status"] == "down" and not step["usable"]


def test_open_model_backups_skip_a_step_the_check_found_down_but_try_everything_when_all_are():
    from app.shared import open_fallbacks as of

    keys = ["text:a:1", "text:b:2"]
    probe._down.clear()
    probe._down["text:a:1"] = "retired"
    try:
        assert of._in_turn(keys) == [1]
        probe._down["text:b:2"] = "down"
        assert of._in_turn(keys) == [0, 1]
    finally:
        probe._down.clear()
