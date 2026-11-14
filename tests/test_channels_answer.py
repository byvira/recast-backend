"""A short written answer from what a read finished with: lines, lists, secrets left out, long text never copied, and only reads are answered in the chat."""
from app.channels import answer
from app.tools.registry import get_registry


def read_tool():
    from app.tools.contract import Risk

    return next(s.name for s in get_registry().all() if s.risk is Risk.READ and not s.name.startswith(("compose.", "changes.")))


def write_tool():
    from app.tools.contract import Risk

    return next(s.name for s in get_registry().all() if s.risk is Risk.WRITE)


def run_of(tool, data, state="done"):
    return {"result": {"steps": [{"tool": tool, "state": state, "result": {"data": data}}]}}


def test_numbers_words_and_one_level_of_nesting_become_short_lines():
    text = answer.short_answer(run_of(read_tool(), {"plan": "Starter", "credits_left": 1200, "limits": {"pictures_day": 5, "campaigns_active": 1}, "on_trial": False}))
    assert text.splitlines() == ["Plan: Starter", "Credits left: 1,200", "Limits:", "  Pictures day: 5", "  Campaigns active: 1", "On trial: no"]


def test_a_list_is_counted_and_its_first_titles_are_named():
    text = answer.short_answer(run_of(read_tool(), {"posts": [{"title": "Launch day"}, {"name": "Tips"}, {"id": "x"}]}))
    assert text == "Posts: 3 items: Launch day, Tips"
    assert answer.short_answer(run_of(read_tool(), {"posts": []})) == ""


def test_secrets_and_long_text_are_never_copied():
    text = answer.short_answer(run_of(read_tool(), {"api_key": "sk-123", "token": "abc", "content": "x" * 500, "title": "Short"}))
    assert text == "Title: Short"


def test_the_answer_is_short_and_a_run_that_changed_something_gets_no_answer_text():
    big = {f"field_{i}": f"value {i}" for i in range(60)}
    text = answer.short_answer(run_of(read_tool(), big))
    assert len(text.splitlines()) <= answer.MAX_LINES and len(text) <= answer.MAX_CHARS
    assert answer.short_answer(run_of(write_tool(), {"title": "A draft"})) == ""
    assert answer.short_answer(run_of(read_tool(), {"a": 1}, state="failed")) == "" and answer.short_answer({}) == ""
