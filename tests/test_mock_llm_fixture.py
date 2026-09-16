"""Self-test for the mock_llm fixture (conftest.py) — Module 2's shared LLM
mock layer. If this fixture were silently broken (patching the wrong
module, or not patching at all), every test in Stages 3-7 that relies on
it would give false confidence — either by accident calling a real Groq
API, or by every assertion trivially passing against an untouched
function. Confirms both: the patch actually replaces the real function,
and overriding a response via the handle actually changes what the
patched module's own call returns.
"""


async def test_mock_llm_patches_call_llm_in_chips_module(mock_llm):
    import app.pipelines.text.chips as chips_module

    result = await chips_module.call_llm("any prompt")
    # Default is the fixture's own plausible-length placeholder, not empty.
    assert isinstance(result, str)
    assert len(result) > 10


async def test_mock_llm_set_plain_changes_chips_module_response(mock_llm):
    import app.pipelines.text.chips as chips_module

    mock_llm.set_plain("A specific override string")
    result = await chips_module.call_llm("any prompt")
    assert result == "A specific override string"


async def test_mock_llm_patches_call_llm_structured_in_scorer_module(mock_llm):
    import app.pipelines.text.scorer as scorer_module

    mock_llm.set_structured({"score": 91, "reason": "strong hook"})
    result = await scorer_module.call_llm_structured("any prompt")
    assert result == {"score": 91, "reason": "strong hook"}


async def test_mock_llm_patches_call_llm_chat_in_refiner_module(mock_llm):
    import app.pipelines.text.refiner as refiner_module

    mock_llm.set_chat("Refined via chat.")
    result = await refiner_module.call_llm_chat(messages=[{"role": "user", "content": "shorten this"}])
    assert result == "Refined via chat."


async def test_mock_llm_patches_generator_module_both_functions(mock_llm):
    import app.pipelines.text.generator as generator_module

    mock_llm.set_plain("Plain generation output.")
    mock_llm.set_structured({"content": "structured output"})

    plain_result = await generator_module.call_llm("prompt")
    structured_result = await generator_module.call_llm_structured("prompt")

    assert plain_result == "Plain generation output."
    assert structured_result == {"content": "structured output"}


async def test_mock_llm_never_touches_the_real_groq_client(mock_llm, monkeypatch):
    """Belt-and-suspenders: if any patch above were wired to the wrong
    module and fell through to the real implementation, this would try to
    build a real Groq client and fail fast on a missing/invalid call
    rather than silently succeeding against a live API."""
    import app.shared.llm as llm_module

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("get_groq_client() was called — mock_llm did not intercept a real LLM call path")

    monkeypatch.setattr(llm_module, "get_groq_client", _fail_if_called)

    import app.pipelines.text.chips as chips_module
    import app.pipelines.text.scorer as scorer_module

    await chips_module.call_llm("prompt")
    await scorer_module.call_llm_structured("prompt")
