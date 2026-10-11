"""The hook step does not overwrite a title or an approved opener, honours banned words, and survives a bad recommended number."""
from app.pipelines.text.hook_agent import apply_recommended_hook, clamp_index

HOOKS = [
    {"text": "Most posts fail on the first line.", "score": 8},
    {"text": "Your draft has one job in line one.", "score": 7},
    {"text": "Stop using the free tier now.", "score": 6},
]
BODY = "Plain opening that is fine.\n\nSecond paragraph with the rest of the point."


def test_a_blog_title_is_never_replaced():
    blog = "# How we plan a week of posts\n\nIntro paragraph that follows the title."
    assert apply_recommended_hook(blog, HOOKS, 0) == blog


def test_an_approved_opener_is_kept():
    text = "Here is the thing. The rest of the post follows.\n\nMore."
    assert apply_recommended_hook(text, HOOKS, 0, approved_openers=["Here is the thing"]) == text


def test_a_hook_with_a_banned_word_is_skipped_for_the_next_best():
    out = apply_recommended_hook(BODY, HOOKS, 0, banned_words=["fail"])
    assert out.startswith("Your draft has one job in line one.")


def test_a_recommended_number_that_is_out_of_range_or_text_does_not_raise():
    assert apply_recommended_hook(BODY, HOOKS, 7).startswith(HOOKS[0]["text"])
    assert apply_recommended_hook(BODY, HOOKS, "1").startswith(HOOKS[1]["text"])
    assert clamp_index(None, 3) == 0 and clamp_index("x", 3) == 0 and clamp_index(-1, 3) == 0
