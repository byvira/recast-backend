"""Ops, Website content: drafts never reach the website, a writer sends for review, an approver puts live (now or at a time), the
legal pages need the Master Admin, every live version is kept and can be put back, and the rich text is cleaned on save."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.site_content import store as C
from tests.test_ops_team import _join, _master

BODY = "<p>" + " ".join(["Ekavo learns how you write from your own posts and keeps every format in that voice."] * 4) + "</p>"


def _post(title: str) -> dict:
    return {"title": title, "summary": "What changes when writing starts from you.", "slug": "", "author": "Asha", "html": BODY}


async def _writer(master, signup_user):
    res = await master.post("/api/v1/ops/team/roles", json={
        "name": f"Writer {uuid4().hex[:6]}", "permissions": {"content": ["edit"]}, "reason": "Writes drafts"})
    assert res.status_code == 200, res.text
    return await _join(master, signup_user, "Writer", "staff", res.json()["role"]["id"])


async def test_a_post_goes_from_draft_to_review_to_live_and_only_live_is_public(signup_user, make_client):
    master, _ = await _master(signup_user)
    writer, _ = await _writer(master, signup_user)
    visitor = make_client()
    title = f"Why posts sound the same {uuid4().hex[:5]}"
    res = await writer.post("/api/v1/ops/content", json={"kind": "post", "data": _post(title)})
    assert res.status_code == 200, res.text
    item = res.json()["item"]
    assert item["status"] == "draft" and item["key"].startswith("why-posts-sound-the-same")
    pid, slug = item["id"], item["key"]
    # a draft never reaches the website
    assert (await visitor.get(f"/api/v1/public/content/posts/{slug}")).status_code == 404
    # the writer cannot put it live, only send it for review
    assert (await writer.post(f"/api/v1/ops/content/{pid}/publish", json={})).status_code == 403
    res = await writer.post(f"/api/v1/ops/content/{pid}/review", json={"note": "Please check the second heading"})
    assert res.status_code == 200 and res.json()["item"]["status"] == "review"
    listed = (await master.get("/api/v1/ops/content", params={"kind": "post", "status": "review"})).json()["items"]
    assert any(r["id"] == pid for r in listed)
    res = await master.post(f"/api/v1/ops/content/{pid}/publish", json={"note": "Looks good"})
    assert res.status_code == 200 and res.json()["item"]["status"] == "live"
    public = (await visitor.get(f"/api/v1/public/content/posts/{slug}")).json()["post"]
    assert public["title"] == title and public["read_minutes"] >= 1
    assert any(p["slug"] == slug for p in (await visitor.get("/api/v1/public/content/posts")).json()["posts"])
    # a later change stays a draft; the website keeps the live copy
    item = (await master.get(f"/api/v1/ops/content/{pid}")).json()["item"]
    res = await writer.put(f"/api/v1/ops/content/{pid}/draft", json={"data": {**item["draft"], "title": "A new title"}, "base_version": item["draft_version"]})
    assert res.status_code == 200 and res.json()["item"]["status"] == "draft"
    assert (await visitor.get(f"/api/v1/public/content/posts/{slug}")).json()["post"]["title"] == title
    # a save from an older copy is refused, so two people never overwrite each other
    stale = await master.put(f"/api/v1/ops/content/{pid}/draft", json={"data": item["draft"], "base_version": item["draft_version"]})
    assert stale.status_code == 409


async def test_rich_text_is_cleaned_and_copy_rules_stop_a_publish(signup_user):
    master, _ = await _master(signup_user)
    html = BODY + '<script>alert(1)</script><p onclick="x()">Hi</p><iframe src="https://evil.example/x"></iframe>' \
                  '<iframe src="https://www.youtube.com/embed/abc"></iframe><img src="http://insecure.example/a.png">'
    res = await master.post("/api/v1/ops/content", json={"kind": "post", "data": {**_post("Clean me"), "html": html}})
    saved = res.json()["item"]["draft"]["html"]
    assert "<script" not in saved and "onclick" not in saved and "evil.example" not in saved and "insecure" not in saved
    assert "youtube.com/embed/abc" in saved
    pid = res.json()["item"]["id"]
    item = res.json()["item"]
    res = await master.put(f"/api/v1/ops/content/{pid}/draft", json={"data": {**item["draft"], "summary": "Fast — and loud"}, "base_version": item["draft_version"]})
    assert any("long dash" in p["message"] for p in res.json()["item"]["problems"])
    res = await master.post(f"/api/v1/ops/content/{pid}/publish", json={})
    assert res.status_code == 400 and "long dash" in res.json()["detail"]


async def test_a_set_time_goes_live_by_itself_and_a_change_cancels_it(signup_user, make_client):
    master, _ = await _master(signup_user)
    pid = (await master.post("/api/v1/ops/content", json={"kind": "post", "data": _post(f"Later {uuid4().hex[:4]}")})).json()["item"]["id"]
    when = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    item = (await master.post(f"/api/v1/ops/content/{pid}/publish", json={"at": when})).json()["item"]
    assert item["status"] == "scheduled"
    assert (await make_client().get(f"/api/v1/public/content/posts/{item['key']}")).status_code == 404
    # its time comes
    await C.content_col().update_one({"id": pid}, {"$set": {"schedule_at": datetime.now(timezone.utc) - timedelta(minutes=1)}})
    assert await C.promote_due(force=True) == 1
    assert (await master.get(f"/api/v1/ops/content/{pid}")).json()["item"]["status"] == "live"
    # a change after setting a time takes it back to a draft
    pid2 = (await master.post("/api/v1/ops/content", json={"kind": "post", "data": _post(f"Moved {uuid4().hex[:4]}")})).json()["item"]["id"]
    item = (await master.post(f"/api/v1/ops/content/{pid2}/publish", json={"at": when})).json()["item"]
    res = await master.put(f"/api/v1/ops/content/{pid2}/draft", json={"data": {**item["draft"], "summary": "Changed after it was set."}, "base_version": item["draft_version"]})
    assert res.json()["item"]["status"] == "draft" and res.json()["item"]["schedule_at"] is None


async def test_every_live_version_is_kept_and_one_can_be_put_back(signup_user, make_client):
    master, _ = await _master(signup_user)
    item = (await master.post("/api/v1/ops/content", json={"kind": "post", "data": _post(f"Versions {uuid4().hex[:4]}")})).json()["item"]
    pid = item["id"]
    await master.post(f"/api/v1/ops/content/{pid}/publish", json={})
    item = (await master.get(f"/api/v1/ops/content/{pid}")).json()["item"]
    await master.put(f"/api/v1/ops/content/{pid}/draft", json={"data": {**item["draft"], "summary": "Second summary for the post."}, "base_version": item["draft_version"]})
    await master.post(f"/api/v1/ops/content/{pid}/publish", json={})
    versions = (await master.get(f"/api/v1/ops/content/{pid}/versions")).json()["versions"]
    assert [v["version"] for v in versions] == [2, 1]
    res = await master.post(f"/api/v1/ops/content/{pid}/restore", json={"version": 1})
    assert res.status_code == 200 and res.json()["item"]["draft"]["summary"] == _post("x")["summary"]
    assert res.json()["item"]["status"] == "draft"
    # taking it down, then deleting it
    assert (await master.delete(f"/api/v1/ops/content/{pid}")).status_code == 400
    await master.post(f"/api/v1/ops/content/{pid}/unpublish", json={"note": "Old"})
    assert (await make_client().get(f"/api/v1/public/content/posts/{item['key']}")).status_code == 404
    assert (await master.delete(f"/api/v1/ops/content/{pid}")).status_code == 200


async def test_pages_and_legal_pages(signup_user, make_client):
    master, _ = await _master(signup_user)
    visitor = make_client()
    await C.content_col().delete_many({"kind": "page"})
    assert (await visitor.get("/api/v1/public/content/pages/pricing")).status_code == 404
    pricing = {"intro": {"eyebrow": "Pricing", "title": "Plans that *grow.*", "description": "Start free."},
               "blurbs": {"free": "Try it.", "single": "For one.", "duo": "For two.", "large": "For teams."}, "featured": "duo",
               "credit_note": "Some features use credits.", "faq": [{"id": "a", "question": "Is there a free plan?", "answer": "Yes."}]}
    item = (await master.post("/api/v1/ops/content", json={"kind": "page", "key": "pricing", "data": pricing})).json()["item"]
    assert (await master.post("/api/v1/ops/content", json={"kind": "page", "key": "pricing", "data": pricing})).status_code == 409
    assert (await master.post(f"/api/v1/ops/content/{item['id']}/publish", json={})).status_code == 200
    assert (await visitor.get("/api/v1/public/content/pages/pricing")).json()["page"]["blurbs"]["duo"] == "For two."
    assert (await master.post("/api/v1/ops/content", json={"kind": "page", "key": "home", "data": {}})).status_code == 404
    # a legal page goes live only by the Master Admin
    marketing, _ = await _join(master, signup_user, "Market", "staff", "marketing")
    legal = {"title": "Privacy", "effective": "2026-11-02", "html": BODY}
    lid = (await marketing.post("/api/v1/ops/content", json={"kind": "page", "key": "privacy", "data": legal})).json()["item"]["id"]
    assert (await marketing.post(f"/api/v1/ops/content/{lid}/publish", json={})).status_code == 403
    assert (await master.post(f"/api/v1/ops/content/{lid}/publish", json={})).status_code == 200
    assert (await visitor.get("/api/v1/public/content/pages/privacy")).status_code == 200
    summary = (await master.get("/api/v1/ops/content/summary")).json()
    assert {"pricing", "privacy"} <= set(summary["pages"])


async def test_jobs_need_a_way_to_apply_and_closed_jobs_leave_the_list(signup_user, make_client):
    master, _ = await _master(signup_user)
    job = {"title": f"Content lead {uuid4().hex[:4]}", "summary": "Lead our writing.", "team": "Content", "place": "Remote in India",
           "job_type": "full_time", "html": BODY, "closes_on": ""}
    item = (await master.post("/api/v1/ops/content", json={"kind": "job", "data": job})).json()["item"]
    res = await master.post(f"/api/v1/ops/content/{item['id']}/publish", json={})
    assert res.status_code == 400 and "apply" in res.json()["detail"]
    res = await master.put(f"/api/v1/ops/content/{item['id']}/draft", json={"data": {**item["draft"], "apply_email": "jobs@example.com"}, "base_version": item["draft_version"]})
    assert (await master.post(f"/api/v1/ops/content/{item['id']}/publish", json={})).status_code == 200
    visitor = make_client()
    assert any(j["slug"] == item["key"] for j in (await visitor.get("/api/v1/public/content/jobs")).json()["jobs"])
    await C.content_col().update_one({"id": item["id"]}, {"$set": {"live.closes_on": "2020-01-01"}})
    assert not any(j["slug"] == item["key"] for j in (await visitor.get("/api/v1/public/content/jobs")).json()["jobs"])
    assert (await visitor.get(f"/api/v1/public/content/jobs/{item['key']}")).json()["job"]["open"] is False


async def test_only_staff_with_the_area_reach_ops_content(signup_user):
    master, _ = await _master(signup_user)
    customer, _ = await signup_user("Customer")
    assert (await customer.get("/api/v1/ops/content")).status_code == 403
    viewer, _ = await _join(master, signup_user, "Looker", "staff", "viewer")
    assert (await viewer.post("/api/v1/ops/content", json={"kind": "post", "data": _post("Nope")})).status_code == 403
    engineer, _ = await _join(master, signup_user, "Builder", "staff", "engineer")
    assert (await engineer.get("/api/v1/ops/content")).status_code == 403


def test_what_the_editor_writes_comes_back_the_same_after_cleaning():
    """Each element the post editor can make keeps the parts the editor needs to open it again."""
    from app.site_content.html_clean import clean_html

    editor_html = (
        '<h3 style="text-align: center">Head</h3><p><span style="color: #ff6a3d">red</span> <mark data-color="#ff0" '
        'style="background-color: #ff0; color: inherit">hi</mark> <u>u</u> <s>s</s> <sup>2</sup> <sub>2</sub> <code>c</code></p>'
        '<ul data-type="taskList"><li data-type="taskItem" data-checked="true"><label><input type="checkbox" checked><span></span></label>'
        '<div><p>done</p></div></li></ul>'
        '<img src="https://res.cloudinary.com/x/a.png" alt="A chart" data-align="left">'
        '<div data-youtube-video=""><iframe src="https://www.youtube-nocookie.com/embed/abc" width="640" height="480" allowfullscreen="true"></iframe></div>'
        '<video src="https://res.cloudinary.com/x/v.mp4" controls="true"></video><audio src="https://res.cloudinary.com/x/a.mp3" controls="true"></audio>'
        '<table><tbody><tr><th colspan="1" rowspan="1"><p>A</p></th></tr><tr><td colspan="1" rowspan="1"><p>B</p></td></tr></tbody></table>'
        '<pre><code class="language-js">x()</code></pre><blockquote><p>q</p></blockquote><hr>'
        '<p><a href="https://example.com" target="_blank" rel="noopener noreferrer nofollow">link</a> <a href="mailto:a@b.co">mail</a></p>'
    )
    out = clean_html(editor_html)
    for kept in ('style="text-align: center"', 'style="color: #ff6a3d"', 'background-color: #ff0', 'data-type="taskList"', 'data-checked="true"',
                 'data-align="left"', 'alt="A chart"', 'data-youtube-video', 'youtube-nocookie.com/embed/abc', '<video', '<audio', '<th', 'colspan',
                 '<pre>', '<blockquote>', '<hr>', 'target="_blank"', 'mailto:a@b.co', '<u>', '<s>', '<sup>', '<sub>'):
        assert kept in out, kept
    assert "<input" not in out and "color: inherit" not in out and "nofollow" not in out


async def test_many_posts_take_one_action_and_each_one_left_says_why(signup_user):
    master, _ = await _master(signup_user)
    writer, _ = await _writer(master, signup_user)
    ids = []
    for n in range(3):
        res = await writer.post("/api/v1/ops/content", json={"kind": "post", "data": _post(f"Batch post {n} {uuid4().hex[:5]}")})
        ids.append(res.json()["item"]["id"])
    # a writer sends for review in one go, but can't put live
    res = await writer.post("/api/v1/ops/content/bulk", json={"ids": ids, "action": "review"})
    assert res.status_code == 200 and sorted(res.json()["done"]) == sorted(ids)
    assert (await writer.post("/api/v1/ops/content/bulk", json={"ids": ids, "action": "publish"})).status_code == 403
    res = await master.post("/api/v1/ops/content/bulk", json={"ids": ids[:2], "action": "publish", "note": "Batch"})
    assert sorted(res.json()["done"]) == sorted(ids[:2])
    # a live post isn't deleted; the others are, and the reason comes back
    res = (await master.post("/api/v1/ops/content/bulk", json={"ids": ids, "action": "delete"})).json()
    assert res["done"] == [ids[2]] and {s["id"] for s in res["skipped"]} == set(ids[:2])
    assert "Take it off the website first" in res["skipped"][0]["reason"]
    res = (await master.post("/api/v1/ops/content/bulk", json={"ids": ids[:2], "action": "unpublish"})).json()
    assert sorted(res["done"]) == sorted(ids[:2])
    listed = (await master.get("/api/v1/ops/content", params={"kind": "post", "status": "live"})).json()["items"]
    assert not any(r["id"] in ids for r in listed)
