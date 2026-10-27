"""Prompts moved out of Python code into prompt files, and prompts that were read once at start-up and are now read on each use,
send exactly what they sent before: every one is compared byte for byte with a recording made from the code before the move.
No model is called; the call is replaced by a recorder."""
import json
import pathlib

import pytest

SNAPSHOT = json.loads((pathlib.Path(__file__).parent / "fixtures" / "prompt_moves_snapshot.json").read_text(encoding="utf-8"))


def test_fixed_prompts_read_from_their_files_are_unchanged():
    from app.agents.personal import assist
    from app.api.v1 import search, support_assistant
    from app.prompts.registry import load_prompt, render_fragment

    files = {
        "content_guard/screen_system": "content_guard/screen_system",
        "content_guard/rewrite_system": "content_guard/rewrite_system",
        "brand/extract_traits_system": "brand/extract_traits_system",
        "brand/preview_rewrite_system": "brand/preview_rewrite_system",
        "brand/suggest_voice_patterns_system": "brand/suggest_voice_patterns_system",
    }
    for key, name in files.items():
        assert render_fragment(name) == SNAPSHOT[key], key
    assert load_prompt("content_guard/picture_unsafe") == SNAPSHOT["content_guard/picture_unsafe"]
    assert load_prompt("content_guard/picture_writing") == SNAPSHOT["content_guard/picture_writing"]
    assert assist._align_system() == SNAPSHOT["personal/align_system"]
    assert search._agentic_system() == SNAPSHOT["search/agentic_system"]
    assert support_assistant._system() == SNAPSHOT["support/assistant_system"]


@pytest.fixture
def recorder(monkeypatch):
    import app.shared.llm as llm

    seen: list[dict] = []

    async def structured(**kw):
        seen.append(kw)
        return {"unsafe": False}

    async def text(*a, **kw):
        seen.append({"args": a, **kw})
        return "ok"

    monkeypatch.setattr(llm, "call_llm_structured", structured)
    monkeypatch.setattr(llm, "call_llm", text)
    return seen


async def test_the_safety_check_and_rewrite_prompts_are_unchanged(recorder):
    from app.agents.content_guard import agent

    for i, post in enumerate(["Our spring sale starts Monday. Come early!", "Use {{ brand }} and {% raw %} now {# x #}"]):
        agent._model_verdicts.clear()
        recorder.clear()
        await agent._model_screen(post)
        assert recorder[0]["prompt"] == SNAPSHOT[f"content_guard/screen#{i}"]
        assert recorder[0]["system"] == SNAPSHOT["content_guard/screen_system"]
    for i, (post, cats) in enumerate([("Buy now {{x}}", ["hate", "violence"]), ("Plain post", ["sexual"])]):
        recorder.clear()
        await agent._rewrite(post, cats)
        assert recorder[0]["prompt"] == SNAPSHOT[f"content_guard/rewrite#{i}"]
        assert recorder[0]["system"] == SNAPSHOT["content_guard/rewrite_system"]


async def test_the_picture_checks_send_the_same_words(monkeypatch):
    from app.agents.content_guard import media
    import app.shared.llm as llm

    seen = []

    async def vision(**kw):
        seen.append(kw["prompt"])
        return '{"unsafe": false, "has_text": false}'

    async def on():
        return True

    monkeypatch.setattr(llm, "call_vision", vision)
    monkeypatch.setattr(media, "_enabled", on)
    media._VERDICTS.clear()
    media._WRITING.clear()
    await media.check_image_bytes(b"picture-one")
    await media.picture_has_writing(b"picture-two")
    assert seen == [SNAPSHOT["content_guard/picture_unsafe"], SNAPSHOT["content_guard/picture_writing"]]


def test_typing_suggestions_ask_with_the_same_words_for_every_kind():
    from app.pipelines import suggest

    for kind in [*suggest.KINDS, "unknown"]:
        for hint in ("", "Acme; calm; bold {{ x }}"):
            assert list(suggest.prompt_for(kind, "We are launching our new", hint)) == SNAPSHOT[f"suggest/complete#{kind}#{bool(hint)}"]


async def test_youtube_metadata_asks_with_the_same_words_with_and_without_a_transcript(monkeypatch):
    from app.pipelines.publish.youtube import metadata

    seen = []

    async def record(*a, **kw):
        seen.append(a[0] if a else kw.get("prompt"))
        return "TITLE: t\nTAGS: a, b"

    monkeypatch.setattr(metadata, "call_llm", record)
    brand = {"identity": {"name": "Bean Co"}, "brand_type": "Business"}
    media = {"transcript": [{"word": w, "start_s": i, "end_s": i + 0.5} for i, w in enumerate("we roast beans {{ slowly }}".split())]}
    for i, m in enumerate([None, media]):
        seen.clear()
        await metadata.generate_youtube_metadata("How we roast beans {{ x }}\nSecond line", brand, media=m)
        assert seen[0] == SNAPSHOT[f"youtube/metadata#{i}"]


async def test_the_json_retry_asks_with_the_same_words(monkeypatch):
    import app.shared.llm as llm

    replies = iter(["not json at all {{ x }}", '{"a": 1}'])
    seen = []

    async def record(**kw):
        seen.append(kw)
        return next(replies)

    monkeypatch.setattr(llm, "call_llm_fallback", record)
    await llm.call_llm_structured_fallback(prompt="Give me the plan {{ y }}", system="Be brief")
    assert [seen[0]["system"], seen[1]["prompt"]] == SNAPSHOT["llm/json_repair"]


def test_prompts_once_read_at_start_up_are_read_on_use_and_unchanged():
    from app.agents.supervisor import nodes
    from app.pipelines.media import image_generation as ig
    from app.pipelines.text import chips, generator
    from app.prompts.registry import load_prompt
    import app.shared.llm as llm

    assert nodes._synth_instructions() == SNAPSHOT["import/supervisor_synth"]
    assert load_prompt("media/image_describe") == SNAPSHOT["import/image_describe"]
    assert ig.no_text_suffix() == ig.NO_TEXT_SUFFIX == SNAPSHOT["import/image_no_text"]
    assert ig.soft_screen() == ig.SOFT_SCREEN == SNAPSHOT["import/image_soft_screen"]
    assert ig.screen_suffix() == ig.SCREEN_SUFFIX == SNAPSHOT["import/image_screen_suffix"]
    assert generator.specificity_instruction() == generator.SPECIFICITY_INSTRUCTION == SNAPSHOT["import/text_specificity"]
    assert generator.engagement_patterns() == generator.ENGAGEMENT_PATTERNS == SNAPSHOT["import/text_engagement"]
    assert llm._json_system_suffix() == SNAPSHOT["import/json_suffix"]
    assert dict(chips.CHIP_PROMPTS) == SNAPSHOT["import/chips"]
    assert "shorten" in chips.CHIP_PROMPTS and "nope" not in chips.CHIP_PROMPTS and chips.CHIP_PROMPTS.get("nope") is None


def test_a_changed_prompt_file_is_used_without_a_restart(tmp_path, monkeypatch):
    """The point of reading on each use: an edited file (later, the live Ops version) reaches the next call."""
    from jinja2 import FileSystemLoader

    from app.pipelines.media import image_generation as ig
    from app.prompts import registry

    (tmp_path / "media").mkdir()
    (tmp_path / "media" / "image_no_text.jinja").write_text("no words, please", encoding="utf-8")
    monkeypatch.setattr(registry._env, "loader", FileSystemLoader(str(tmp_path)))
    registry._env.cache.clear() if registry._env.cache else None
    assert ig.no_text_suffix() == "no words, please"


def test_no_prompt_text_is_left_written_inside_a_model_call():
    """Reads the code: a model call whose prompt or system text is written in Python, instead of read from a prompt file, fails
    here, so a new one cannot slip back in. Short labels (under 26 characters) and plain variables are fine."""
    import ast

    app = pathlib.Path(__file__).resolve().parent.parent / "app"
    calls = {"call_llm", "call_llm_structured", "call_llm_chat", "call_vision", "call_llm_fallback", "call_llm_structured_fallback",
             "call_llm_stream"}
    found = []
    for path in app.rglob("*.py"):
        if "prompts" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name not in calls:
                continue
            for value in [*node.args, *(k.value for k in node.keywords if k.arg in ("prompt", "system"))]:
                if isinstance(value, ast.JoinedStr) or (isinstance(value, ast.Constant) and isinstance(value.value, str) and len(value.value) > 25):
                    found.append(f"{path.relative_to(app.parent).as_posix()}:{node.lineno}")
    assert found == []
