"""One-tap links, email in and the digest. Resend and every model are replaced by stand-ins: no email is sent and no credits are spent."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.db.mongo import get_db
from app.tools import approvals, digest, email_in, ledger, links
from tests.conftest import create_workspace

R = "/api/v1/tool-reach"
PL = "/api/v1/public/links"


@pytest.fixture
async def world(signup_user, monkeypatch):
    client, profile = await signup_user()
    ws = await create_workspace(client, f"Reach {uuid4().hex[:6]}")
    for index in (links.create_indexes, email_in.create_indexes, ledger.create_indexes):
        await index()
    sent: list[tuple[str, str, dict]] = []

    async def fake_send(template_id, to, variables, *a, **k):
        sent.append((template_id, to, variables))
        return True

    monkeypatch.setattr(email_in, "send_templated_email", fake_send, raising=False)
    import app.core.notifications as notifications
    import app.tools.digest as digest_module

    monkeypatch.setattr(notifications, "send_templated_email", fake_send)
    monkeypatch.setattr(digest_module, "send_templated_email", fake_send)
    return client, ws, profile, {"X-Workspace-Id": ws}, sent


async def _piece(ws, **over) -> str:
    pid = uuid4().hex
    await get_db()["content_pieces"].insert_one({"piece_id": pid, "id": pid, "workspace_id": ws, "platform": "LinkedIn", "content": "We launch on Friday.", "approval_status": "pending",
                                                 "created_at": datetime.now(timezone.utc), **over})
    return pid


def _token(url: str) -> str:
    return url.rsplit("/", 1)[1]


# ── one-tap links ────────────────────────────────────────────────────────

async def test_opening_a_link_only_shows_it_and_the_button_does_it_once(world):
    client, ws, profile, h, sent = world
    pid = await _piece(ws)
    url = await links.make(ws, profile["id"], "approve_post", pid)
    for _ in range(3):                                                                    # a mail scanner opening the link again and again
        shown = await client.get(f"{PL}/{_token(url)}")
        assert shown.status_code == 200 and shown.json()["post"]["text"] == "We launch on Friday." and shown.json()["button"] == "Yes, approve it"
    assert (await get_db()["content_pieces"].find_one({"piece_id": pid}))["approval_status"] == "pending"      # nothing happened

    done = await client.post(f"{PL}/{_token(url)}/do")
    assert done.status_code == 200 and "Approved" in done.json()["message"]
    assert (await get_db()["content_pieces"].find_one({"piece_id": pid}))["approval_status"] == "approved"
    again = await client.post(f"{PL}/{_token(url)}/do")
    assert again.status_code == 410
    assert (await client.get(f"{PL}/{_token(url)}")).status_code == 410


async def test_a_bad_expired_or_used_link_all_answer_the_same_and_only_a_hash_is_kept(world):
    client, ws, profile, h, sent = world
    pid = await _piece(ws)
    url = await links.make(ws, profile["id"], "approve_post", pid)
    stored = await links.links().find_one({"workspace_id": ws})
    assert _token(url) not in str(stored) and stored["hash"] != _token(url)
    await links.links().update_one({"id": stored["id"]}, {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}})
    answers = {(await client.get(f"{PL}/{t}")).text for t in (_token(url), "nonsense", "x" * 200)}
    assert len(answers) == 1 and "already been used, or it has ended" in answers.pop()
    assert (await client.post(f"{PL}/{_token(url)}/do")).status_code == 410
    assert (await get_db()["content_pieces"].find_one({"piece_id": pid}))["approval_status"] == "pending"


async def test_a_link_acts_as_the_person_with_their_role_now_and_is_not_used_up_by_a_refusal(world):
    client, ws, profile, h, sent = world
    pid = await _piece(ws)
    url = await links.make(ws, profile["id"], "approve_post", pid)
    await get_db()["workspace_members"].update_one({"workspace_id": ws, "user_id": profile["id"]}, {"$set": {"role": "editor"}})     # an editor cannot approve
    refused = await client.post(f"{PL}/{_token(url)}/do")
    assert refused.status_code == 403
    assert (await get_db()["content_pieces"].find_one({"piece_id": pid}))["approval_status"] == "pending"
    await get_db()["workspace_members"].update_one({"workspace_id": ws, "user_id": profile["id"]}, {"$set": {"role": "owner"}})
    assert (await client.post(f"{PL}/{_token(url)}/do")).status_code == 200                      # the same link works once the role is back
    await get_db()["workspace_members"].update_one({"workspace_id": ws, "user_id": profile["id"]}, {"$set": {"status": "removed"}})
    other = await links.make(ws, profile["id"], "approve_post", await _piece(ws))
    assert (await client.post(f"{PL}/{_token(other)}/do")).status_code in (401, 403)              # a removed person's link does nothing


async def test_send_back_and_the_yes_or_no_to_a_waiting_call(world):
    client, ws, profile, h, sent = world
    pid = await _piece(ws)
    back = await links.make(ws, profile["id"], "send_back", pid)
    assert (await client.post(f"{PL}/{_token(back)}/do")).status_code == 200
    assert (await get_db()["content_pieces"].find_one({"piece_id": pid}))["approval_status"] == "rejected"

    request = {"id": uuid4().hex, "workspace_id": ws, "credential_id": "k", "tool": "publish.now", "arguments": {"piece_id": pid}, "fingerprint": "fp", "status": "pending",
               "created_at": datetime.now(timezone.utc), "preview": {"action": "Publish one approved post now."}}
    await ledger.approval_requests().insert_one(dict(request))
    shown = await client.get(f"{PL}/{_token(await links.make(ws, profile['id'], 'yes_send', request['id']))}")
    assert shown.json()["waiting"] is True and shown.json()["preview"]["action"].startswith("Publish")
    yes = await links.make(ws, profile["id"], "yes_send", request["id"])
    assert (await client.post(f"{PL}/{_token(yes)}/do")).status_code == 200
    done = await ledger.approval_requests().find_one({"id": request["id"]})
    assert done["status"] == "approved" and done["token"]                                         # the caller can now collect the one-time token


# ── email in ─────────────────────────────────────────────────────────────

async def _brand(ws):
    await get_db()["brand_profiles"].insert_one({"id": uuid4().hex, "workspace_id": ws, "user_id": "u", "brand_type": "Business", "identity": {"name": "Acme"}, "is_default": True, "is_complete": True,
                                                 "default_platform": "LinkedIn", "platforms": ["LinkedIn"], "created_at": datetime.now(timezone.utc)})


def _mail(address, sender, **over):
    return {"to": [address], "from": f"Someone <{sender}>", "email_id": uuid4().hex, "subject": "Launch idea", **over}


async def _full(email_id, text="Our launch is on Friday. Here is why it matters.", **over):
    return {"text": text, "subject": "Launch idea", **over}


async def test_a_forwarded_message_becomes_a_free_draft_with_buttons_and_no_credits_are_used(world):
    client, ws, profile, h, sent = world
    await _brand(ws)
    address = await email_in.rotate(ws)
    assert "+d-" in address and address.endswith("@" + email_in.inbound_domain())
    outcome = await email_in.handle(_mail(address, profile["email"]), _full)
    assert outcome == "saved"
    piece = await get_db()["content_pieces"].find_one({"workspace_id": ws, "content": {"$regex": "launch is on Friday"}})
    assert piece and piece["platform"] == "LinkedIn"
    template, to, variables = sent[-1]
    assert template == "email-in-saved" and to == profile["email"] and variables["approve_url"].startswith("http") and variables["send_back_url"]
    stored = await get_db()["email_in_addresses"].find_one({"workspace_id": ws})
    assert address.split("+d-")[1].split("@")[0] not in str(stored)                              # only a hash of the code is kept
    approve = await client.post(f"{PL}/{_token(variables['approve_url'])}/do")
    assert approve.status_code == 200                                                            # approval is by the button, never by replying


async def test_a_spoofed_or_unknown_message_creates_nothing_and_says_nothing(world):
    client, ws, profile, h, sent = world
    await _brand(ws)
    address = await email_in.rotate(ws)
    before = await get_db()["content_pieces"].count_documents({"workspace_id": ws})
    assert await email_in.handle(_mail(address, "stranger@example.com"), _full) == "sender_not_a_member"
    assert await email_in.handle(_mail(address, profile["email"]), lambda i: _full(i, headers={"authentication-results": "mx; spf=fail; dkim=pass"})) == "sender_failed_checks"
    assert await email_in.handle(_mail("drafts+d-" + "z" * 24 + "@x.com", profile["email"]), _full) == "unknown_address"
    assert await email_in.handle(_mail("support@x.com", profile["email"]), _full) == "not_for_drafts"
    assert await email_in.handle(_mail(address, profile["email"]), lambda i: _full(i, text="")) in ("empty", "saved")
    assert await get_db()["content_pieces"].count_documents({"workspace_id": ws}) in (before, before + 1)
    assert [s for s in sent if s[1] == "stranger@example.com"] == []                              # nothing is sent back to a stranger


async def test_the_same_message_twice_makes_one_draft_and_a_new_address_ends_the_old_one(world):
    client, ws, profile, h, sent = world
    await _brand(ws)
    address = await email_in.rotate(ws)
    mail = _mail(address, profile["email"])
    assert await email_in.handle(mail, _full) == "saved"
    assert await email_in.handle(mail, _full) == "duplicate"
    count = await get_db()["content_pieces"].count_documents({"workspace_id": ws})
    fresh = await email_in.rotate(ws)
    assert fresh != address
    assert await email_in.handle(_mail(address, profile["email"]), _full) == "unknown_address"      # the old address is dead
    assert await get_db()["content_pieces"].count_documents({"workspace_id": ws}) == count


async def test_a_sender_is_held_to_ten_an_hour(world):
    client, ws, profile, h, sent = world
    await _brand(ws)
    address = await email_in.rotate(ws)
    results = [await email_in.handle(_mail(address, profile["email"]), lambda i, n=n: _full(i, text=f"Idea number {n}")) for n in range(email_in.PER_HOUR + 2)]
    assert results.count("saved") == email_in.PER_HOUR and results[-1] == "limit"


async def test_a_workspace_with_no_brand_gets_no_draft_and_the_owner_makes_the_address_in_settings(world):
    client, ws, profile, h, sent = world
    address = await email_in.rotate(ws)
    assert await email_in.handle(_mail(address, profile["email"]), _full) == "no_brand"
    assert (await client.get(f"{R}/email-in", headers=h)).json() == {"has_address": True}
    made = await client.post(f"{R}/email-in/new-address", headers=h)
    assert made.status_code == 201 and "+d-" in made.json()["address"]


async def test_the_signed_support_route_hands_drafts_mail_to_email_in(world, monkeypatch):
    from app.shared import support_inbound

    client, ws, profile, h, sent = world
    seen = []

    async def fake_handle(data, fetch):
        seen.append(data["to"])
        return "saved"

    monkeypatch.setattr(email_in, "handle", fake_handle)
    outcome = await support_inbound.handle_event({"type": "email.received", "data": {"email_id": "abc", "to": ["drafts+d-" + "a" * 20 + "@x.com"]}})
    assert outcome == "saved" and seen


# ── the digest ───────────────────────────────────────────────────────────

async def test_the_digest_lists_what_waits_with_buttons_skips_held_posts_and_sends_nothing_when_quiet(world):
    client, ws, profile, h, sent = world
    assert await digest.build(ws, profile["id"], "owner") is None                                   # nothing waiting, nothing sent
    ok, held = await _piece(ws), await _piece(ws, flagged_for_review=True)
    built = await digest.build(ws, profile["id"], "owner")
    assert built["count"] == 1 and "LinkedIn" in built["items"][0]["title"] and built["items"][0]["approve_url"] and held not in str(built)
    assert await digest.build(ws, profile["id"], "editor") is None                                  # a person who cannot approve is not offered buttons
    await ledger.approval_requests().insert_one({"id": uuid4().hex, "workspace_id": ws, "credential_id": "k", "tool": "publish.now", "arguments": {}, "fingerprint": "f", "status": "pending",
                                                 "created_at": datetime.now(timezone.utc), "preview": {"action": "Publish one approved post now."}})
    both = await digest.build(ws, profile["id"], "owner")
    assert both["count"] == 2 and "publish one approved post now" in both["items"][1]["title"].lower() and ok


async def test_the_digest_is_off_until_switched_on_and_goes_out_once_a_day(world):
    client, ws, profile, h, sent = world
    await _piece(ws)
    assert (await client.get(f"{R}/digest", headers=h)).json() == {"enabled": False}
    await digest.prefs().delete_many({})
    assert await digest.send_digests() == 0                                                         # nobody opted in
    assert (await client.put(f"{R}/digest", headers=h, json={"enabled": True})).json() == {"enabled": True}
    first = await digest.send_digests()
    second = await digest.send_digests()
    assert first >= 1 and second == 0
    template, to, variables = [s for s in sent if s[0] == "approvals-digest"][-1]
    assert to == profile["email"] and int(variables["count"]) >= 1 and "Approve: http" in variables["list"] and "Send back: http" in variables["list"] and variables["open_url"].startswith("http")
    await client.put(f"{R}/digest", headers=h, json={"enabled": False})
    assert (await client.get(f"{R}/digest", headers=h)).json() == {"enabled": False}


async def test_links_for_the_digest_work_once_each_and_approve_the_right_post(world):
    client, ws, profile, h, sent = world
    pid = await _piece(ws)
    item = (await digest.build(ws, profile["id"], "owner"))["items"][0]
    assert (await client.post(f"{PL}/{_token(item['approve_url'])}/do")).status_code == 200
    assert (await get_db()["content_pieces"].find_one({"piece_id": pid}))["approval_status"] == "approved"
    assert approvals is not None


async def test_an_unfinished_brand_gets_no_draft_by_email_the_same_as_in_the_app(world):
    client, ws, profile, h, sent = world
    await get_db()["brand_profiles"].insert_one({"id": uuid4().hex, "workspace_id": ws, "user_id": "u", "brand_type": "Business", "identity": {}, "is_default": True, "is_complete": False,
                                                 "default_platform": "LinkedIn", "platforms": ["LinkedIn"], "created_at": datetime.now(timezone.utc)})
    address = await email_in.rotate(ws)
    assert await email_in.handle(_mail(address, profile["email"]), _full) == "refused"
    assert await get_db()["content_pieces"].count_documents({"workspace_id": ws}) == 0
