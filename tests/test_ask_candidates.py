"""Free-tier decide candidates (L28): other Groq models by name from a setting, and a translator pivot that is off until allowed. No provider is called."""
import pytest

from app.ask import candidates, decisions
from app.core.config import settings
from app.tools.registry import get_registry


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    before = dict(decisions.CANDIDATES)
    yield
    decisions.CANDIDATES.clear()
    decisions.CANDIDATES.update(before)


def two_specs():
    pool = [s for s in get_registry().all() if not s.name.startswith(("compose.", "changes."))]
    return pool[:2]


def test_the_models_to_try_come_from_a_setting_and_are_added_only_when_groq_is_set_up(monkeypatch):
    monkeypatch.setattr(settings, "ASK_CANDIDATE_MODELS", " qwen/qwen3-32b , llama-3.3-70b-versatile ,, ")
    assert candidates.candidate_models() == ["qwen/qwen3-32b", "llama-3.3-70b-versatile"]
    monkeypatch.setattr(settings, "GROQ_API_KEY", "")
    assert candidates.register() == [] or all(not n.startswith("groq:") for n in candidates.register())
    monkeypatch.setattr(settings, "GROQ_API_KEY", "k")
    names = candidates.register()
    assert "groq:qwen/qwen3-32b" in names and "groq:llama-3.3-70b-versatile" in names and "groq:qwen/qwen3-32b" in decisions.CANDIDATES


async def test_a_groq_candidate_is_a_closed_choice_that_cannot_name_a_tool_outside_the_list(monkeypatch):
    first, second = two_specs()
    seen: list[str] = []

    async def fake(model, prompt, system):
        seen.append(model)
        return {"action": "tool", "tool": first.name}

    monkeypatch.setattr(candidates, "_groq_json", fake)
    decide = candidates.groq_candidate("qwen/qwen3-32b")
    got = await decide("what is on", [first, second], "en")
    assert got == decisions.Decision("tool", first.name) and seen == ["qwen/qwen3-32b"]

    async def outside(model, prompt, system):
        return {"action": "tool", "tool": "publish.now"}

    monkeypatch.setattr(candidates, "_groq_json", outside)
    assert await decide("post it", [first, second], "en") == decisions.Decision("none")

    async def broken(model, prompt, system):
        raise RuntimeError("quota")

    monkeypatch.setattr(candidates, "_groq_json", broken)
    with pytest.raises(RuntimeError):
        await decide("x", [first], "en")                                                    # a failure is the harness's to count, never hidden


def test_the_translator_pivot_is_off_unless_allowed_and_set_up(monkeypatch):
    monkeypatch.setattr(settings, "ASK_PIVOT_ENABLED", False)
    monkeypatch.setattr(settings, "CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setattr(settings, "CLOUDFLARE_ACCOUNT_ID", "a")
    candidates.register()
    assert "pivot_m2m100" not in decisions.CANDIDATES
    monkeypatch.setattr(settings, "ASK_PIVOT_ENABLED", True)
    assert "pivot_m2m100" in candidates.register()
    monkeypatch.setattr(settings, "CLOUDFLARE_API_TOKEN", "")
    decisions.CANDIDATES.pop("pivot_m2m100")
    assert "pivot_m2m100" not in candidates.register()


async def test_the_pivot_translates_another_language_first_and_leaves_english_alone(monkeypatch):
    first, second = two_specs()
    translated: list[tuple[str, str]] = []
    decided: list[tuple[str, str]] = []

    async def translate(text, source):
        translated.append((text, source))
        return "how many credits are left"

    async def live(text, specs, language):
        decided.append((text, language))
        return decisions.Decision("tool", first.name)

    monkeypatch.setattr(candidates, "_translate", translate)
    monkeypatch.setattr(decisions, "live", live)
    got = await candidates.pivot_m2m100("இந்த மாதம் எனக்கு எத்தனை கிரெடிட் மீதம்", [first, second], "ta")
    assert got.tool == first.name and translated == [("இந்த மாதம் எனக்கு எத்தனை கிரெடிட் மீதம்", "ta")] and decided == [("how many credits are left", "en")]
    translated.clear()
    await candidates.pivot_m2m100("how many credits are left", [first], "en")
    assert translated == [] and decided[-1] == ("how many credits are left", "en")             # English is never sent to the translator


async def test_a_translation_failure_is_raised_so_the_harness_counts_it(monkeypatch):
    async def down(text, source):
        raise RuntimeError("translator down")

    monkeypatch.setattr(candidates, "_translate", down)
    with pytest.raises(RuntimeError):
        await candidates.pivot_m2m100("नमस्ते", two_specs(), "hi")
