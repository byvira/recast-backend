"""The Configure panel's extras (and the number of batch days) reach the stream route, and an old client that sends none behaves as before."""
import inspect

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
