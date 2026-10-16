"""A reader's reaction to a post: the reply is tidied to a fixed shape, and the route asks the model only when the writer asks, never saves anything,
refuses a post too short to react to, and keeps to the writer's own workspace and brands. The model is replaced, so no real call is made."""
from app.db.mongo import brand_profiles, content_pieces
from app.pipelines.text import reader_review as R
from tests.conftest import create_workspace


# ── tidying the model's reply (no database) ──────────────────────────────────────

def test_a_good_reply_is_kept_in_its_shape():
    out = R.tidy({
        "headline": "I learned the launch moved to May.",
        "verdict": "almost",
        "strengths": ["The first line made me stop."],
        "fixes": [{"where": "the second paragraph", "problem": "I lost track of who 'we' is.", "suggestion": "Name the team once."}],
        "questions": ["What does it cost?"],
    })
    assert out == {
        "headline": "I learned the launch moved to May.", "verdict": "almost", "strengths": ["The first line made me stop."],
        "fixes": [{"where": "the second paragraph", "problem": "I lost track of who 'we' is.", "suggestion": "Name the team once."}],
        "questions": ["What does it cost?"],
    }


def test_too_much_and_odd_values_are_cut_down():
    out = R.tidy({
        "headline": "h" * 900, "verdict": "AMAZING!!!", "strengths": ["a", "b", "c", "d", "e"],
        "fixes": [{"problem": f"p{i}", "suggestion": f"s{i}"} for i in range(9)] + ["not a dict", {"where": "x"}],
        "questions": [f"q{i}" for i in range(7)],
    })
    assert len(out["headline"]) <= 240 and len(out["strengths"]) == 3 and len(out["fixes"]) == 4 and len(out["questions"]) == 3
    assert out["verdict"] == "needs_work"  # four fixes and an unknown verdict: worked out, not invented


def test_a_missing_verdict_is_worked_out_from_what_the_reader_said():
    assert R.tidy({"headline": "Clear.", "strengths": ["Short."]})["verdict"] == "ready"
    assert R.tidy({"headline": "Mostly clear.", "fixes": [{"problem": "p", "suggestion": "s"}]})["verdict"] == "almost"


def test_a_reply_with_nothing_in_it_is_no_reply():
    assert R.tidy({}) is None
    assert R.tidy("a string") is None
    assert R.tidy({"verdict": "ready", "fixes": [{"where": "only a place"}]}) is None


# ── the route ────────────────────────────────────────────────────────────────────

async def _brand(client, ws):
    res = await client.post("/api/v1/brand/", json={"brand_type": "Person"}, headers={"X-Workspace-Id": ws})
    assert res.status_code in (200, 201), res.text
    brand_id = res.json()["brand_profile_id"]
    await brand_profiles.update_one({"id": brand_id}, {"$set": {"is_complete": True}})
    return brand_id


async def test_a_reader_reaction_is_returned_tidied_and_nothing_is_saved(signup_user, mock_llm):
    client, _ = await signup_user()
    ws = await create_workspace(client, "Reader WS", tier="large")
    brand_id = await _brand(client, ws)
    mock_llm.set_structured({
        "headline": "I took away that the launch is in May.", "verdict": "almost", "strengths": ["Clear dates."],
        "fixes": [{"where": "the end", "problem": "No next step.", "suggestion": "Say where to sign up."}], "questions": ["Is it free?"],
    })
    before = await content_pieces.count_documents({"workspace_id": ws})
    res = await client.post(
        "/api/v1/text/reader-review",
        json={"content": "We are launching in May with three new features for small teams.", "platform": "LinkedIn", "brand_id": brand_id},
        headers={"X-Workspace-Id": ws},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["verdict"] == "almost" and body["fixes"][0]["suggestion"] == "Say where to sign up." and body["questions"] == ["Is it free?"]
    assert await content_pieces.count_documents({"workspace_id": ws}) == before


async def test_a_post_too_short_to_react_to_is_refused_before_any_model_call(signup_user, mock_llm):
    client, _ = await signup_user()
    ws = await create_workspace(client, "Reader WS", tier="large")
    brand_id = await _brand(client, ws)
    res = await client.post("/api/v1/text/reader-review", json={"content": "Hi there", "platform": "LinkedIn", "brand_id": brand_id}, headers={"X-Workspace-Id": ws})
    assert res.status_code == 400 and "a few words" in res.json()["detail"]


async def test_an_empty_model_reply_says_so_instead_of_inventing_one(signup_user, mock_llm):
    client, _ = await signup_user()
    ws = await create_workspace(client, "Reader WS", tier="large")
    brand_id = await _brand(client, ws)
    mock_llm.set_structured({})
    res = await client.post(
        "/api/v1/text/reader-review",
        json={"content": "We are launching in May with three new features for small teams.", "platform": "LinkedIn", "brand_id": brand_id},
        headers={"X-Workspace-Id": ws},
    )
    assert res.status_code == 502 and "Try again" in res.json()["detail"]


async def test_another_workspaces_brand_cannot_be_used(signup_user, mock_llm):
    client, _ = await signup_user()
    ws = await create_workspace(client, "Reader WS", tier="large")
    brand_id = await _brand(client, ws)
    other, _ = await signup_user()
    other_ws = await create_workspace(other, "Elsewhere", tier="large")
    mock_llm.set_structured({"headline": "x", "verdict": "ready"})
    res = await other.post(
        "/api/v1/text/reader-review",
        json={"content": "We are launching in May with three new features for small teams.", "platform": "LinkedIn", "brand_id": brand_id},
        headers={"X-Workspace-Id": other_ws},
    )
    assert res.status_code == 404
