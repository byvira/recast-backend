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
    embeddings = next(c for c in chains.describe(healthy) if c["id"] == "embeddings")
    assert embeddings["depth"] == "thin" and embeddings["usable"] == 1

    down = {**healthy, "gemini": {"configured": True, "label": "Gemini", "status": "down"}}
    assert next(c for c in chains.describe(down) if c["id"] == "embeddings")["depth"] == "broken"

    voice = next(c for c in chains.describe(healthy) if c["id"] == "voice")
    assert voice["depth"] == "ok" and [s["provider"] for s in voice["steps"]][-1] == "gemini"


def test_a_step_that_is_switched_off_or_has_no_key_is_not_counted():
    states = {n: {"configured": True, "label": n, "status": "healthy"} for n in chains.providers_in_chains()}
    states["deepgram"]["configured"] = False
    voice = next(c for c in chains.describe(states) if c["id"] == "voice")
    deepgram = next(s for s in voice["steps"] if s["provider"] == "deepgram")
    assert deepgram["status"] == "not_set" and deepgram["usable"] is False
