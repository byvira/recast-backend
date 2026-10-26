"""Model calls are labelled by feature for LLM health without each call site naming it."""

from app.shared.llm_health.context import UNKNOWN_FEATURE, feature_for, llm_context, current_feature, set_prompt_path, set_request_path


def test_prompt_folder_names_the_feature():
    assert feature_for("text/generate/linkedin", None) == "text"
    assert feature_for("media/image_headline", None) == "image"
    assert feature_for("supervisor/synthesis", None) == "odette"
    assert feature_for("personal/align_draft", None) == "remy"


def test_shared_fragments_fall_through_to_the_route():
    assert feature_for("fragments/retry_feedback", "/api/v1/search/agentic") == "search"


def test_nothing_known_stays_unknown():
    assert feature_for(None, "/api/v1/workspaces") is None


def test_an_explicit_label_wins():
    set_prompt_path("text/generate/linkedin")
    set_request_path("/api/v1/search")
    with llm_context(feature="campaigns"):
        assert current_feature() == "campaigns"
    assert current_feature() == "text"
    set_prompt_path(None)
    set_request_path(None)
    assert current_feature() == UNKNOWN_FEATURE


def test_empty_and_blocked_answers_are_reported_to_llm_health(monkeypatch):
    """An empty answer or one the content filter stopped is a failure people see; it must reach LLM health, not only the log."""
    from types import SimpleNamespace

    from app.shared import llm

    noted = []
    monkeypatch.setattr(llm, "note_app_failure", lambda kind, *a, **k: noted.append(kind))

    def reply(text, reason):
        return SimpleNamespace(model="m", usage=None, choices=[SimpleNamespace(finish_reason=reason, message=SimpleNamespace(content=text))])

    llm._log_if_truncated(reply("", "length"), "test")
    llm._log_if_truncated(reply("partial", "content_filter"), "test")
    llm._log_if_truncated(reply("a real answer", "stop"), "test")
    assert noted == ["empty_response", "content_blocked"]
