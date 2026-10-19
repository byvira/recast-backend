"""The Configure panel's extras (and the number of batch days) reach the stream route, and an old client that sends none behaves as before."""
import inspect

import pytest

from app.api.v1 import text_stream
from app.models.text import ExtrasConfig

TOGGLES = ("hook_variations", "hashtags", "auto_cta", "seo_meta", "grammar_check", "plagiarism_check", "avoid_blacklist", "pdf_export")


def _route_params() -> dict:
    handler = next(
        r.endpoint for r in text_stream.router.routes
        if getattr(r, "path", "").endswith("/stream") and "GET" in getattr(r, "methods", set())
    )
    return dict(inspect.signature(handler).parameters)


def test_every_extra_toggle_and_the_batch_days_are_accepted_by_the_stream_route():
    params = _route_params()
    for name in (*TOGGLES, "batch_days"):
        assert name in params, f"the stream route does not read {name}"
        # None means the client did not say: the server's own default then applies.
        assert params[name].default.default is None


def test_a_toggle_the_client_leaves_out_keeps_the_servers_default():
    defaults = ExtrasConfig()
    assert ExtrasConfig(**{}) == defaults
    changed = ExtrasConfig(auto_cta=not defaults.auto_cta)
    assert changed.auto_cta is (not defaults.auto_cta)
    for name in TOGGLES:
        if name != "auto_cta":
            assert getattr(changed, name) == getattr(defaults, name)


def test_the_plagiarism_toggle_is_accepted_and_does_nothing_else():
    assert "plagiarism_check" in ExtrasConfig.model_fields
    assert ExtrasConfig(plagiarism_check=True).plagiarism_check is True


async def test_a_run_left_paused_ends_by_itself_and_keeps_what_was_made(monkeypatch):
    import asyncio

    from app.agents.text import event_emitter

    monkeypatch.setattr(event_emitter, "MAX_PAUSE_SECONDS", 0.05)
    emitter = event_emitter.EventEmitter()
    emitter.completed_platforms.append("linkedin")
    emitter.request_pause()

    with pytest.raises(asyncio.CancelledError):
        await emitter.pause_point()

    kinds = []
    while not emitter.queue.empty():
        item = emitter.queue.get_nowait()
        kinds.append(item["type"] if isinstance(item, dict) else "done")
    assert "pipeline_cancelled" in kinds


async def test_every_platform_of_one_run_shares_one_cleaned_source_and_brief(monkeypatch):
    import asyncio

    from app.agents.text import event_emitter, nodes

    calls = {"clean": 0, "brief": 0}

    async def clean(raw):
        calls["clean"] += 1
        await asyncio.sleep(0.01)
        return raw.strip()

    async def brief(text, language="en"):
        calls["brief"] += 1
        return "the brief"

    monkeypatch.setattr(nodes, "clean_raw_content", clean)
    monkeypatch.setattr(nodes, "extract_content_brief", brief)
    emitter = event_emitter.EventEmitter()

    got = await asyncio.gather(*[nodes._normalised_once(emitter, "  the same source  ", "en") for _ in range(4)])

    assert calls == {"clean": 1, "brief": 1}               # four platforms, two model calls, not eight
    assert len({g for g in got}) == 1
    other = await nodes._normalised_once(emitter, "a different source", "en")
    assert other[0] == "a different source" and calls["clean"] == 2


def test_too_many_hashtags_are_counted_and_reported():
    from app.models.text import Platform
    from app.pipelines.text.generator import limit_warnings

    crowded = "A short post. " + " ".join(f"#tag{n}" for n in range(40))
    notes = limit_warnings(Platform.LINKEDIN, crowded)
    assert any("hashtags" in n for n in notes)
    assert not any("hashtags" in n for n in limit_warnings(Platform.LINKEDIN, "A short post with #one tag."))
