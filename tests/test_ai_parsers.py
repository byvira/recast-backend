"""Small parsers and fallbacks around model replies: each keeps a bad or odd reply from reaching the member. No model calls."""
from unittest.mock import AsyncMock

from app.pipelines.brand import trait_extraction
from app.pipelines.media import headline
from app.pipelines.text.seo import shorten_meta


def test_a_long_meta_description_is_cut_at_a_whole_word():
    meta = "A plain description of the post that goes on and on about the planning of a full week of posts " * 3
    out = shorten_meta(meta)
    assert len(out) <= 160 and out.endswith("...")
    assert out[:-3].split()[-1] in meta.split(), "the cut is at a whole word"
    assert shorten_meta("Short and fine.") == "Short and fine."


async def test_trait_extraction_keeps_only_short_text_traits_and_survives_a_bad_reply(monkeypatch):
    monkeypatch.setattr(trait_extraction, "set_usage_workspace", lambda *_: None)
    reply = {"traits": ["Short sentences", "", "x" * 500, 42, "Opens with a question", "a", "b", "c", "d"]}
    monkeypatch.setattr(trait_extraction, "call_llm_structured", AsyncMock(return_value=reply))
    traits = await trait_extraction.extract_sample_traits("Some writing sample that is long enough to analyse.")
    assert len(traits) <= 5 and "" not in traits and all(len(t) <= 60 for t in traits)
    monkeypatch.setattr(trait_extraction, "call_llm_structured", AsyncMock(return_value={}))
    assert await trait_extraction.extract_sample_traits("Anything at all here.") == []
    monkeypatch.setattr(trait_extraction, "call_llm_structured", AsyncMock(side_effect=RuntimeError("down")))
    assert await trait_extraction.extract_sample_traits("Anything at all here.") == []


async def test_a_headline_that_invents_a_number_falls_back_to_the_posts_first_sentence(monkeypatch):
    import app.shared.llm as llm

    monkeypatch.setattr(llm, "call_llm", AsyncMock(return_value="Cut costs by 73 percent today"))
    out = await headline.make_headline("We plan a week of posts in one sitting. The rest follows from that.")
    assert "73" not in out and out.startswith("We plan a week")


async def test_a_failing_headline_call_still_gives_a_headline(monkeypatch):
    import app.shared.llm as llm

    monkeypatch.setattr(llm, "call_llm", AsyncMock(side_effect=RuntimeError("down")))
    out = await headline.make_headline("Batch your content on Sundays. It saves the whole week.")
    assert out.startswith("Batch your content")
