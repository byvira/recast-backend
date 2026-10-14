"""The image agent: the same gates and provider chain as before, now a planned, logged workflow on the shared runtime.

The model-facing leaf calls (prompt polish, safety verdict, brand fit, the picture provider, storage) are replaced, as the other
image tests do, so no provider quota is spent; the graph, the tool registry, the decision log and the library writes are real."""
from uuid import uuid4

import pytest

from app.agents.image import nodes
from app.agents.runtime import PlanError
from app.db.mongo import get_db
from app.pipelines.media import image_generation as ig

BRAND = {"visual_identity": {"logo_url": "", "visual_style_notes": ""}, "name": "Acme"}


@pytest.fixture
def leaves(monkeypatch):
    """Controls for the leaf calls, and a record of what they were asked."""
    calls = {"polish": [], "generate": []}
    cfg = {"verdict": "safe", "generic_ok": True, "fit_ok": True, "picture": b"jpeg-bytes", "failure": None, "qa": (False, None),
           "generic_fail_first": False}

    async def polish(raw):
        calls["polish"].append(raw)
        return f"A calm desk with soft morning light (try {len(calls['polish'])})"

    async def safety(prompt):
        return cfg["verdict"]

    def generic(prompt):
        if cfg["generic_fail_first"] and len(calls["polish"]) == 1:
            return False
        return cfg["generic_ok"]

    async def fit(prompt, brand):
        return cfg["fit_ok"]

    async def make(prompt):
        calls["generate"].append(prompt)
        if cfg["picture"] is None:
            ig._fail(cfg["failure"] or "No picture service could make this picture.")
        return cfg["picture"]

    async def qa(image, brand):
        return cfg["qa"]

    async def uploaded(data, content_type, user_id, filename=None):
        return f"https://res.cloudinary.com/x/image/upload/v1/{uuid4()}.jpg"

    monkeypatch.setattr(ig, "_polish_prompt", polish)
    monkeypatch.setattr(ig, "_safety_verdict", safety)
    monkeypatch.setattr(ig, "_anti_generic_gate", generic)
    monkeypatch.setattr(ig, "_brand_fit_gate", fit)
    monkeypatch.setattr(ig, "_generate_text_free", make)
    monkeypatch.setattr(ig, "_qa_gate", qa)
    monkeypatch.setattr(ig, "_image_provider_configured", lambda: True)
    monkeypatch.setattr("app.shared.storage.upload_file", uploaded)
    return SimpleCfg(cfg, calls)


class SimpleCfg:
    def __init__(self, cfg, calls):
        self.cfg, self.calls = cfg, calls

    def __getitem__(self, key):
        return self.cfg[key]

    def __setitem__(self, key, value):
        self.cfg[key] = value


async def _decisions(ws: str) -> list[dict]:
    return await get_db()["agent_decisions"].find({"workspace_id": ws, "agent": "image"}).to_list(10)


# ── the registry and the plan ────────────────────────────────────────────────────────────────────────────────────────────

def test_a_plan_may_only_use_registered_tools():
    assert nodes.REGISTRY.validate_plan(["gate", "generate"]) == ["gate", "generate"]
    with pytest.raises(PlanError):
        nodes.REGISTRY.validate_plan(["gate", "teleport"])
    with pytest.raises(PlanError):
        nodes.REGISTRY.validate_plan([])


def test_every_tool_states_what_it_costs():
    costs = {n: nodes.REGISTRY.spec(n).cost for n in nodes.REGISTRY.names()}
    assert costs["generate"] == "provider" and costs["gate"] == "llm" and costs["publish"] == "free"


# ── a picture from a prompt ──────────────────────────────────────────────────────────────────────────────────────────────

async def test_a_prompt_becomes_a_picture_and_the_decision_is_logged(leaves):
    ws = f"ws-{uuid4()}"
    out = await ig.generate_image_from_prompt(prompt="a desk", workspace_id=ws, user_id="u1", target_size=(1024, 1024), brand_profile=BRAND)
    assert out == b"jpeg-bytes"
    assert leaves.calls["generate"][0].endswith(ig.NO_TEXT_SUFFIX.strip())      # the no-writing rule is applied
    [entry] = await _decisions(ws)
    assert entry["outcome"] == "ok" and entry["evidence"]["plan"] == ["gate", "generate"]
    assert entry["evidence"]["gates"] == [{"attempt": 1, "passed": True, "reason": None}]


async def test_a_style_gate_that_objects_twice_still_gives_a_picture(leaves):
    leaves["generic_ok"] = False
    ws = f"ws-{uuid4()}"
    out = await ig.generate_image_from_prompt(prompt="a desk", workspace_id=ws, user_id="u1", target_size=(1, 1), brand_profile=BRAND)
    assert out == b"jpeg-bytes"
    [entry] = await _decisions(ws)
    assert entry["evidence"]["style_gate_waived"] is True and len(entry["evidence"]["gates"]) == 2


async def test_a_corrected_second_pass_is_told_what_was_wrong_the_first_time(leaves):
    leaves["generic_fail_first"] = True
    out = await ig.generate_image_from_prompt(prompt="a desk", workspace_id=f"ws-{uuid4()}", user_id="u", target_size=(1, 1), brand_profile=BRAND)
    assert out == b"jpeg-bytes"
    assert len(leaves.calls["polish"]) == 2
    assert "too generic" in leaves.calls["polish"][1]


async def test_an_unsafe_prompt_is_a_hard_stop_with_a_plain_reason(leaves):
    leaves["verdict"] = "unsafe"
    ws = f"ws-{uuid4()}"
    out = await ig.generate_image_from_prompt(prompt="x", workspace_id=ws, user_id="u", target_size=(1, 1), brand_profile=BRAND)
    assert out is None and "safety check" in (ig.last_failure_reason() or "")
    assert leaves.calls["generate"] == []                      # nothing was drawn
    [entry] = await _decisions(ws)
    assert entry["outcome"] == "failed"


async def test_a_safety_check_that_could_not_run_says_so(leaves):
    leaves["verdict"] = "error"
    out = await ig.generate_image_from_prompt(prompt="x", workspace_id=f"ws-{uuid4()}", user_id="u", target_size=(1, 1), brand_profile=BRAND)
    assert out is None and "could not run" in (ig.last_failure_reason() or "")


async def test_a_provider_that_makes_nothing_gives_its_own_reason_to_the_caller(leaves):
    leaves["picture"] = None
    leaves["failure"] = "Cloudflare is not set up. Gemini is not set up."
    out = await ig.generate_image_from_prompt(prompt="x", workspace_id=f"ws-{uuid4()}", user_id="u", target_size=(1, 1), brand_profile=BRAND)
    assert out is None and ig.last_failure_reason() == "Cloudflare is not set up. Gemini is not set up."


# ── a picture from a topic: checked, stored and flagged AI generated ─────────────────────────────────────────────────────

async def test_a_topic_picture_is_stored_flagged_ai_generated_and_logged(leaves):
    ws = f"ws-{uuid4()}"
    asset = await ig.generate_brand_image(topic="morning routines", brand_profile=BRAND, workspace_id=ws, user_id="u1")
    assert asset is not None
    assert asset.origin == "ai" and asset.contains_ai is True and asset.source.value == "ai_generated"
    row = await get_db()["media_assets"].find_one({"id": asset.id, "workspace_id": ws})
    assert row and row["origin"] == "ai" and row["qa_flagged"] is False
    [entry] = await _decisions(ws)
    assert entry["evidence"]["plan"] == ["build_prompt", "gate", "generate", "qa", "publish"]


async def test_a_picture_the_brand_check_dislikes_is_kept_and_flagged_not_dropped(leaves):
    leaves["qa"] = (True, "The colours do not match the brand.")
    asset = await ig.generate_brand_image(topic="t", brand_profile=BRAND, workspace_id=f"ws-{uuid4()}", user_id="u")
    assert asset is not None and asset.qa_flagged is True and "colours" in asset.qa_flag_reason


async def test_asking_for_the_paid_provider_directly_is_still_refused(leaves):
    out = await ig.generate_brand_image(topic="t", brand_profile=BRAND, workspace_id=f"ws-{uuid4()}", user_id="u",
                                        provider=ig.ImageProvider.GEMINI)
    assert out is None and leaves.calls["generate"] == []


async def test_no_picture_means_no_library_item(leaves):
    leaves["picture"] = None
    ws = f"ws-{uuid4()}"
    out = await ig.generate_brand_image(topic="t", brand_profile=BRAND, workspace_id=ws, user_id="u")
    assert out is None
    assert await get_db()["media_assets"].count_documents({"workspace_id": ws}) == 0
