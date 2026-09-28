"""Tests for Remy's member lexicon actually shaping Text generation.

The jargon blacklist/whitelist (Remy's Vocabulary tab) used to be persisted
with zero consumers. Now app.agents.text.nodes.merge_member_lexicon_enforcement
folds them into the same enforcement dict brand rules already use:
  - blacklist -> banned_words (prompt instruction + hard-gate retry)
  - whitelist -> approved_vocabulary (positive "these are cleared" instruction)

Covers both generation modes — the normal LangGraph path (build_context_node)
and the repurpose path, which builds its enforcement independently and, before
this, silently never applied a member's blacklist at all. Real Mongo, no LLM.
"""

from datetime import datetime, timezone
from uuid import uuid4

from app.agents.text.nodes import (
    _extract_enforcement_data,
    build_context_node,
    merge_member_lexicon_enforcement,
)
from app.db.mongo import brand_profiles, member_lexicon
from app.models.text import AgentTask, GeneratedPiece, Platform
from app.pipelines.text import generator as generator_module
from app.pipelines.text import orchestrator as orchestrator_module
from app.pipelines.text.generator import build_approved_vocabulary_instruction


async def _seed_lexicon(ws_id: str, user_id: str, *, blacklist=(), whitelist=()) -> None:
    now = datetime.now(timezone.utc)
    await member_lexicon.insert_one({
        "id": f"{ws_id}:{user_id}", "workspace_id": ws_id, "user_id": user_id,
        "pronunciations": [], "blacklist": list(blacklist), "whitelist": list(whitelist),
        "writing_blueprint": {}, "created_at": now, "updated_at": now,
    })


async def _seed_brand(ws_id: str, *, banned=("synergy",)) -> dict:
    now = datetime.now(timezone.utc)
    doc = {
        "id": f"brand-{uuid4().hex[:8]}", "workspace_id": ws_id,
        "identity": {"name": "Test Brand"},
        "manual_data": {"banned_words": list(banned)},
        "created_at": now, "updated_at": now,
    }
    await brand_profiles.insert_one(dict(doc))
    return doc


def _ids() -> tuple[str, str]:
    return f"ws-{uuid4().hex[:8]}", f"user-{uuid4().hex[:8]}"


# ── the shared merge ─────────────────────────────────────────────────────────

async def test_blacklist_and_whitelist_merge_into_brand_enforcement():
    ws_id, user_id = _ids()
    brand = await _seed_brand(ws_id, banned=["synergy"])
    await _seed_lexicon(ws_id, user_id, blacklist=["leverage", "circle back"], whitelist=["SaaS", "API"])

    enforcement = _extract_enforcement_data(brand)
    assert enforcement["approved_vocabulary"] == []  # default before any merge

    merged = await merge_member_lexicon_enforcement(enforcement, workspace_id=ws_id, user_id=user_id)

    assert merged["banned_words"] == ["synergy", "leverage", "circle back"]  # brand kept, member added
    assert merged["approved_vocabulary"] == ["SaaS", "API"]


async def test_merge_dedupes_and_keeps_order():
    ws_id, user_id = _ids()
    brand = await _seed_brand(ws_id, banned=["synergy"])
    await _seed_lexicon(ws_id, user_id, blacklist=["synergy", "leverage"], whitelist=["API", "API"])

    merged = await merge_member_lexicon_enforcement(
        _extract_enforcement_data(brand), workspace_id=ws_id, user_id=user_id,
    )
    assert merged["banned_words"] == ["synergy", "leverage"]
    assert merged["approved_vocabulary"] == ["API"]


async def test_merge_is_a_noop_without_a_saved_lexicon_or_user():
    ws_id, user_id = _ids()
    brand = await _seed_brand(ws_id, banned=["synergy"])

    no_lexicon = await merge_member_lexicon_enforcement(
        _extract_enforcement_data(brand), workspace_id=ws_id, user_id=user_id,
    )
    assert no_lexicon["banned_words"] == ["synergy"]
    assert no_lexicon["approved_vocabulary"] == []

    no_user = await merge_member_lexicon_enforcement(
        _extract_enforcement_data(brand), workspace_id=ws_id, user_id=None,
    )
    assert no_user["banned_words"] == ["synergy"]


async def test_one_members_lexicon_never_leaks_to_another():
    ws_id, alice = _ids()
    bob = f"user-{uuid4().hex[:8]}"
    brand = await _seed_brand(ws_id, banned=[])
    await _seed_lexicon(ws_id, alice, blacklist=["alice-only-term"], whitelist=["AliceTerm"])

    merged = await merge_member_lexicon_enforcement(
        _extract_enforcement_data(brand), workspace_id=ws_id, user_id=bob,
    )
    assert merged["banned_words"] == []
    assert merged["approved_vocabulary"] == []


# ── the two generation modes ─────────────────────────────────────────────────

async def test_normal_generation_path_applies_the_lexicon():
    ws_id, user_id = _ids()
    brand = await _seed_brand(ws_id, banned=["synergy"])
    await _seed_lexicon(ws_id, user_id, blacklist=["leverage"], whitelist=["SaaS"])

    state = {
        "brand_id": brand["id"], "workspace_id": ws_id, "user_id": user_id,
        "goal": None, "tone": None, "language": "en", "current_platform": "LinkedIn",
        "extras": {}, "emitter": None, "batch_day_index": None,
    }
    result = await build_context_node(state)

    assert "leverage" in result["extras"]["banned_words"]
    assert "synergy" in result["extras"]["banned_words"]
    assert result["extras"]["approved_vocabulary"] == ["SaaS"]


async def test_repurpose_path_applies_the_lexicon(monkeypatch):
    """Regression: _run_repurpose_path built its own enforcement via a direct
    _extract_enforcement_data() call and never merged the member's lexicon,
    so a personal blacklist silently didn't apply when repurposing."""
    ws_id, user_id = _ids()
    brand = await _seed_brand(ws_id, banned=["synergy"])
    await _seed_lexicon(ws_id, user_id, blacklist=["leverage"], whitelist=["SaaS"])

    seen: list[dict] = []

    async def _fake_single_repurpose(*, platform, enforcement, **kwargs):
        seen.append(enforcement)
        return GeneratedPiece(platform=platform, content="ok", word_count=1, char_count=2)

    monkeypatch.setattr(orchestrator_module, "_run_single_repurpose", _fake_single_repurpose)

    pieces = await orchestrator_module._run_repurpose_path(
        platforms=[Platform.TWITTER],
        normalised=object(),
        brand_profile=brand,
        metadata={},
        source_platform=Platform.LINKEDIN,
        schedule_mode="now",
        scheduled_at=None,
        emitter=None,
        session_id=str(uuid4()),
        workspace_id=ws_id,
        user_id=user_id,
    )

    assert len(pieces) == 1
    assert len(seen) == 1
    assert "leverage" in seen[0]["banned_words"]
    assert "synergy" in seen[0]["banned_words"]
    assert seen[0]["approved_vocabulary"] == ["SaaS"]


# ── what the model is actually told ──────────────────────────────────────────

def test_approved_vocabulary_instruction_renders_terms_and_is_empty_when_unset():
    assert build_approved_vocabulary_instruction([]).strip() == ""

    text = build_approved_vocabulary_instruction(["SaaS", "API"])
    assert "APPROVED VOCABULARY" in text
    assert "'SaaS'" in text
    assert "'API'" in text


async def test_generation_prompt_carries_both_the_ban_and_the_approval(monkeypatch):
    """End to end through generate_for_platform: the prompt sent to the LLM
    contains the member's banned term AND their approved term."""
    captured: dict = {}

    async def _fake_structured(prompt, *args, **kwargs):
        captured["prompt"] = prompt
        return {"content": "A clean post about SaaS."}

    monkeypatch.setattr(generator_module, "call_llm_structured", _fake_structured)

    task = AgentTask(
        agent="text",
        platform=Platform.LINKEDIN,
        content="Source content about our product.",
        brand_context="Brand voice: plain and direct.",
        session_id=str(uuid4()),
        retry_count=0,
        metadata={
            "banned_words": ["leverage"],
            "approved_vocabulary": ["SaaS"],
            "language": "en",
        },
    )
    await generator_module.generate_for_platform(task)

    prompt = captured["prompt"]
    assert "NEVER 'leverage'" in prompt
    assert "APPROVED VOCABULARY" in prompt
    assert "'SaaS'" in prompt
