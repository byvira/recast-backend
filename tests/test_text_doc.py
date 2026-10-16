"""The rich form of a post: the block document, its plain and HTML forms, and the two routes that read and save it. The plain text stays
what is published; saving blocks makes a new version exactly like a plain edit."""
import pytest

from app.db.mongo import brand_profiles, content_pieces
from app.pipelines.text import doc as D
from tests.conftest import create_workspace


def _doc(*blocks):
    return {"version": 1, "blocks": list(blocks)}


P = lambda i, t: {"id": f"b{i}", "type": "paragraph", "text": t}  # noqa: E731


# ── the document (no database) ───────────────────────────────────────────────────

def test_a_document_is_checked_and_unknown_fields_are_dropped():
    clean = D.validate({"blocks": [{**P(1, "Hello"), "evil": "<script>"}, {"id": "b2", "type": "heading", "text": "Title", "level": 1}], "x": 1})
    assert clean == {"version": 1, "blocks": [{"id": "b1", "type": "paragraph", "text": "Hello"}, {"id": "b2", "type": "heading", "text": "Title", "level": 1}]}
    for bad in (None, {"blocks": []}, {"blocks": [{"id": "a", "type": "nope"}]}, {"blocks": [P(1, "x"), P(1, "y")]},
                {"blocks": [{"id": "b", "type": "heading", "text": "t", "level": 9}]}, {"blocks": [{"id": "b", "type": "bullets", "items": []}]},
                {"blocks": [{"id": "bad id", "type": "paragraph", "text": ""}]}, {"blocks": [{"id": "b", "type": "paragraph", "text": "x" * 20001}]}):
        with pytest.raises(D.DocError):
            D.validate(bad)


def test_the_plain_text_is_made_from_the_blocks_without_markup():
    doc = D.validate(_doc(
        {"id": "h", "type": "heading", "text": "A **bold** start", "level": 2}, P(1, "Read [our guide](https://example.com/a) _now_."),
        {"id": "l", "type": "bullets", "items": ["one", "**two**"]}, {"id": "n", "type": "numbers", "items": ["first", "second"]},
        {"id": "q", "type": "quote", "text": "Stay curious"}, {"id": "d", "type": "divider"},
    ))
    assert D.to_plain(doc) == ("A bold start\n\nRead our guide (https://example.com/a) now.\n\n- one\n- two\n\n1. first\n2. second\n\n"
                               "“Stay curious”\n\n———")


def test_html_is_escaped_and_only_the_known_tags_are_written():
    doc = D.validate(_doc(P(1, "<script>alert(1)</script> **b** [x](https://a.example/?q=1&r=2) [bad](javascript:alert(1))"),
                          {"id": "l", "type": "bullets", "items": ["_i_"]}))
    out = D.to_html(doc)
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "<strong>b</strong>" in out and 'href="https://a.example/?q=1&amp;r=2"' in out
    assert '<a href="javascript' not in out and "[bad](javascript:alert(1))" in out      # an unsafe address stays as plain text
    assert "<ul><li><em>i</em></li></ul>" in out


def test_an_old_plain_post_opens_as_blocks_with_its_lists_and_nothing_lost():
    blocks = D.from_plain("Opening line\nsecond line\n\n- one\n- two\n\n1. a\n2. b\n\nClosing")["blocks"]
    assert [b["type"] for b in blocks] == ["paragraph", "bullets", "numbers", "paragraph"]
    assert blocks[0]["text"] == "Opening line\nsecond line" and blocks[1]["items"] == ["one", "two"]
    assert D.to_plain(D.validate(D.from_plain("Just one paragraph."))) == "Just one paragraph."
    assert D.from_plain("")["blocks"][0]["text"] == ""


# ── the routes (real database) ───────────────────────────────────────────────────

async def _piece(signup_user, text="First draft."):
    client, _ = await signup_user()
    ws = await create_workspace(client, "Doc WS", tier="large")
    res = await client.post("/api/v1/brand/", json={"brand_type": "Person"}, headers={"X-Workspace-Id": ws})
    brand_id = res.json()["brand_profile_id"]
    await brand_profiles.update_one({"id": brand_id}, {"$set": {"is_complete": True}})
    made = await client.post("/api/v1/text/pieces/manual", json={"platform": "LinkedIn", "brand_id": brand_id, "content": text}, headers={"X-Workspace-Id": ws})
    assert made.status_code == 200, made.text
    return client, ws, made.json()["piece_id"]


async def test_a_post_opens_as_derived_blocks_and_saving_them_makes_one_new_version(signup_user):
    client, ws, pid = await _piece(signup_user, "Intro line.\n\n- alpha\n- beta")
    head = {"X-Workspace-Id": ws}
    opened = (await client.get(f"/api/v1/content/pieces/{pid}/doc", headers=head)).json()
    assert opened["derived"] is True and [b["type"] for b in opened["doc"]["blocks"]] == ["paragraph", "bullets"]

    doc = {"version": 1, "blocks": [{"id": "h", "type": "heading", "text": "A **new** heading", "level": 2}, *opened["doc"]["blocks"]]}
    saved = await client.put(f"/api/v1/content/pieces/{pid}/doc", json={"doc": doc, "base_version": opened["version"]}, headers=head)
    assert saved.status_code == 200, saved.text
    assert saved.json()["piece"]["content"].startswith("A new heading\n\nIntro line.")
    assert saved.json()["version"] == opened["version"] + 1

    again = (await client.get(f"/api/v1/content/pieces/{pid}/doc", headers=head)).json()
    assert again["derived"] is False and again["doc"]["blocks"][0]["text"] == "A **new** heading"


async def test_a_change_of_formatting_only_keeps_the_blocks_without_a_new_version(signup_user):
    client, ws, pid = await _piece(signup_user, "Same words here.")
    head = {"X-Workspace-Id": ws}
    opened = (await client.get(f"/api/v1/content/pieces/{pid}/doc", headers=head)).json()
    doc = {"version": 1, "blocks": [{"id": "b1", "type": "paragraph", "text": "Same **words** here."}]}
    saved = await client.put(f"/api/v1/content/pieces/{pid}/doc", json={"doc": doc, "base_version": opened["version"]}, headers=head)
    assert saved.status_code == 200 and saved.json()["version"] == opened["version"]
    assert (await content_pieces.find_one({"piece_id": pid}))["content_doc"]["blocks"][0]["text"] == "Same **words** here."


async def test_a_plain_edit_afterwards_drops_the_old_blocks_so_the_two_never_disagree(signup_user):
    client, ws, pid = await _piece(signup_user, "Before.")
    head = {"X-Workspace-Id": ws}
    doc = {"version": 1, "blocks": [{"id": "b1", "type": "paragraph", "text": "Rich **before**."}]}
    await client.put(f"/api/v1/content/pieces/{pid}/doc", json={"doc": doc}, headers=head)
    edited = await client.patch(f"/api/v1/content/pieces/{pid}", json={"content": "Totally different text."}, headers=head)
    assert edited.status_code == 200
    assert "content_doc" not in (await content_pieces.find_one({"piece_id": pid}))
    assert (await client.get(f"/api/v1/content/pieces/{pid}/doc", headers=head)).json()["doc"]["blocks"][0]["text"] == "Totally different text."


async def test_bad_blocks_an_old_version_and_a_published_post_are_refused(signup_user):
    client, ws, pid = await _piece(signup_user, "Original.")
    head = {"X-Workspace-Id": ws}
    assert (await client.put(f"/api/v1/content/pieces/{pid}/doc", json={"doc": {"blocks": []}}, headers=head)).status_code == 422
    empty = {"version": 1, "blocks": [{"id": "b1", "type": "paragraph", "text": "   "}]}
    assert (await client.put(f"/api/v1/content/pieces/{pid}/doc", json={"doc": empty}, headers=head)).status_code == 400
    ok = {"version": 1, "blocks": [{"id": "b1", "type": "paragraph", "text": "Changed."}]}
    assert (await client.put(f"/api/v1/content/pieces/{pid}/doc", json={"doc": ok, "base_version": 99}, headers=head)).status_code == 409
    await content_pieces.update_one({"piece_id": pid}, {"$set": {"publish_status": "published"}})
    assert (await client.put(f"/api/v1/content/pieces/{pid}/doc", json={"doc": ok}, headers=head)).status_code == 409


async def test_accepting_an_ai_suggestion_marks_a_hand_written_post_as_a_mix_and_a_plain_save_does_not(signup_user):
    client, ws, pid = await _piece(signup_user, "Written by hand.")
    head = {"X-Workspace-Id": ws}
    before = (await client.get(f"/api/v1/content/pieces/{pid}", headers=head)).json()
    assert before["origin"] == "own" and before["contains_ai"] is False

    opened = (await client.get(f"/api/v1/content/pieces/{pid}/doc", headers=head)).json()
    doc = {"version": 1, "blocks": [{"id": "b1", "type": "paragraph", "text": "Written by hand, tightened."}]}
    plain = await client.put(f"/api/v1/content/pieces/{pid}/doc", json={"doc": doc, "base_version": opened["version"]}, headers=head)
    assert plain.status_code == 200, plain.text
    assert (await client.get(f"/api/v1/content/pieces/{pid}", headers=head)).json()["origin"] == "own"

    doc2 = {"version": 1, "blocks": [{"id": "b1", "type": "paragraph", "text": "Written by hand, with AI help."}]}
    assisted = await client.put(f"/api/v1/content/pieces/{pid}/doc", json={"doc": doc2, "base_version": plain.json()["version"], "ai_assisted": True}, headers=head)
    assert assisted.status_code == 200, assisted.text
    after = (await client.get(f"/api/v1/content/pieces/{pid}", headers=head)).json()
    assert after["origin"] == "mixed" and after["contains_ai"] is True


# ── long-form: tables and block attributes ───────────────────────────────────────

def _long(*blocks):
    return {"version": 1, "long_form": True, "blocks": list(blocks)}


def test_a_table_is_kept_written_as_lines_and_as_html():
    doc = D.validate(_long({"id": "t1", "type": "table", "header": True, "rows": [["Plan", "Price"], ["Free", "**0**"]]}))
    assert doc["long_form"] is True and doc["blocks"][0]["rows"][1] == ["Free", "**0**"]
    assert D.to_plain(doc) == "Plan | Price\nFree | 0"
    html = D.to_html(doc)
    assert "<th>Plan</th>" in html and "<td><strong>0</strong></td>" in html


def test_tables_need_long_form_mode_and_even_rows():
    table = {"id": "t1", "type": "table", "rows": [["a", "b"]]}
    with pytest.raises(D.DocError):
        D.validate({"version": 1, "blocks": [table]})
    with pytest.raises(D.DocError):
        D.validate(_long({"id": "t1", "type": "table", "rows": [["a", "b"], ["c"]]}))


def test_class_and_anchor_are_written_and_unsafe_ones_are_refused():
    doc = D.validate(_long({"id": "b1", "type": "heading", "level": 2, "text": "Pricing", "attrs": {"class": "lead big", "anchor": "pricing"}}))
    assert D.to_html(doc) == '<h2 id="pricing" class="lead big">Pricing</h2>'
    for bad in ({"class": 'x" onclick="y'}, {"anchor": "1bad"}, {"anchor": "a b"}, {"class": "<script>"}):
        with pytest.raises(D.DocError):
            D.validate(_long({"id": "b1", "type": "paragraph", "text": "x", "attrs": bad}))


def test_an_anchor_can_be_used_once():
    with pytest.raises(D.DocError):
        D.validate(_long(
            {"id": "b1", "type": "paragraph", "text": "x", "attrs": {"anchor": "top"}},
            {"id": "b2", "type": "paragraph", "text": "y", "attrs": {"anchor": "top"}},
        ))
