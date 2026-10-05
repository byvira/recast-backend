"""The topic research note is not trusted as evidence, and a rewrite goes back through hooks and SEO before it is checked again."""
from app.agents.text import nodes
from app.agents.text.graph import build_single_platform_graph
from app.pipelines.text import normalizer


async def test_the_research_note_asks_for_no_invented_figures_and_uses_only_the_brand_facts(monkeypatch):
    seen: list[str] = []

    async def _capture(prompt, **kwargs):
        seen.append(prompt)
        return "A brief."

    monkeypatch.setattr(normalizer, "call_llm", _capture)

    await normalizer.research_topic("cold email tips", "en", "We serve 40 dentists in Pune.")
    await normalizer.research_topic("cold email tips", "en", "")

    with_facts, without_facts = seen
    assert "Do not state any statistic, percentage, price, date, named study" in with_facts
    assert "the only source of specifics" in with_facts and "40 dentists in Pune" in with_facts
    assert "WHAT THE BRAND HAS TOLD US" not in without_facts
    assert "cold email tips" in without_facts


def test_a_rewrite_goes_back_through_the_hooks_and_seo_steps():
    edges = {(e.source, e.target) for e in build_single_platform_graph().get_graph().edges}
    assert ("rewrite", "generate_hooks") in edges
    assert ("generate_hooks", "generate_seo") in edges
    assert ("generate_seo", "quality_check") in edges
    assert ("rewrite", "quality_check") not in edges


def test_a_failed_check_gets_one_rewrite_and_then_is_flagged():
    assert nodes.route_after_quality({"quality_passed": True, "retry_count": 0, "quality_issues": []}) == "passed"
    assert nodes.route_after_quality({"quality_passed": False, "retry_count": 0, "quality_issues": ["too long"]}) == "retry"
    assert nodes.route_after_quality({"quality_passed": False, "retry_count": 1, "quality_issues": ["too long"]}) == "flag"


def test_a_post_in_the_wrong_language_gets_one_extra_try():
    wrong_language = ["The post is mostly English but Tamil was asked for."]
    assert nodes.route_after_quality({"quality_passed": False, "retry_count": 1, "quality_issues": wrong_language}) == "retry"
    assert nodes.route_after_quality({"quality_passed": False, "retry_count": 2, "quality_issues": wrong_language}) == "flag"
