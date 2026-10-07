"""Staff view of waitlist leads and contact messages: the gate, filters, statuses, notes, export, replies and staff email."""

import pytest

from app.db.mongo import contact_messages, lead_events, users, waitlist_leads
from tests.conftest import signup_new_user, unique_email

L = "/api/v1/ops/leads"


@pytest.fixture(autouse=True)
async def _clean():
    yield
    await waitlist_leads.delete_many({"email": {"$regex": "@example.com$"}})
    await contact_messages.delete_many({"email": {"$regex": "@example.com$"}})
    await lead_events.delete_many({"actor_name": "Leads Staff"})


async def _staff(make_client):
    client = make_client()
    user = await signup_new_user(client, name="Leads Staff")
    await users.update_one({"id": user["id"]}, {"$set": {"is_platform_staff": True}})
    return client, user


async def _lead(api_client, **extra):
    email = unique_email()
    await api_client.post("/api/v1/waitlist", json={"email": email, "source": "test", **extra})
    return email, await waitlist_leads.find_one({"email": email.lower()})


async def _message(api_client, **over):
    body = {"name": "Maya", "email": unique_email(), "message": "We are a team of twelve and need more seats.", "topic": "general", **over}
    res = await api_client.post("/api/v1/contact", json=body)
    assert res.status_code == 200
    return body, res.json()


async def test_only_staff_can_open_any_of_it(make_client, api_client):
    client = make_client()
    await signup_new_user(client, name="Not Staff")
    for path in ("/overview", "/waitlist", "/contact"):
        assert (await client.get(L + path)).status_code in (401, 403)
    assert (await api_client.get(L + "/overview")).status_code in (401, 403)


async def test_a_new_message_gets_a_reference_and_starts_as_new(api_client):
    _, first = await _message(api_client)
    _, second = await _message(api_client)
    assert first["reference"].startswith("C-") and first["reference"] != second["reference"]


async def test_the_waitlist_list_filters_by_status_and_search(make_client, api_client):
    client, _ = await _staff(make_client)
    email, lead = await _lead(api_client)
    other, _o = await _lead(api_client)
    await client.patch(f"{L}/waitlist/{lead['id']}", json={"status": "reviewed"})
    reviewed = (await client.get(f"{L}/waitlist", params={"status": "reviewed", "q": email.split("@")[0]})).json()
    assert [row["email"] for row in reviewed["items"]] == [email.lower()]
    joined = (await client.get(f"{L}/waitlist", params={"status": "joined", "q": other.split("@")[0]})).json()
    assert joined["total"] == 1


async def test_an_older_lead_without_a_status_counts_as_joined(make_client, api_client):
    client, _ = await _staff(make_client)
    email, lead = await _lead(api_client)
    await waitlist_leads.update_one({"id": lead["id"]}, {"$unset": {"status": ""}})
    res = (await client.get(f"{L}/waitlist", params={"status": "joined", "q": email.split("@")[0]})).json()
    assert res["total"] == 1 and res["items"][0]["status"] == "joined"


async def test_staff_can_set_hold_but_not_a_status_the_invite_flow_owns(make_client, api_client):
    client, _ = await _staff(make_client)
    _, lead = await _lead(api_client)
    assert (await client.patch(f"{L}/waitlist/{lead['id']}", json={"status": "hold"})).json()["status"] == "hold"
    assert (await client.patch(f"{L}/waitlist/{lead['id']}", json={"status": "invited"})).status_code == 422
    await waitlist_leads.update_one({"id": lead["id"]}, {"$set": {"status": "activated"}})
    assert (await client.patch(f"{L}/waitlist/{lead['id']}", json={"status": "hold"})).status_code == 409


async def test_notes_and_status_changes_leave_a_trail(make_client, api_client):
    client, _ = await _staff(make_client)
    _, lead = await _lead(api_client)
    await client.patch(f"{L}/waitlist/{lead['id']}", json={"status": "reviewed", "note": "Looks like a good fit."})
    detail = (await client.get(f"{L}/waitlist/{lead['id']}")).json()
    assert detail["notes"][0]["text"] == "Looks like a good fit."
    assert {event["action"] for event in detail["events"]} == {"status", "note"}


async def test_the_export_makes_formula_looking_cells_harmless(make_client, api_client):
    client, _ = await _staff(make_client)
    email, lead = await _lead(api_client)
    await waitlist_leads.update_one({"id": lead["id"]}, {"$set": {"source": "=HYPERLINK(1)"}})
    res = await client.get(f"{L}/waitlist/export", params={"q": email.split("@")[0]})
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/csv")
    assert "'=HYPERLINK(1)" in res.text and ",=HYPERLINK" not in res.text


async def test_team_size_messages_come_first_in_the_inbox(make_client, api_client):
    client, _ = await _staff(make_client)
    plain, _p = await _message(api_client, topic="general")
    sales, _s = await _message(api_client, topic="seats")
    items = (await client.get(f"{L}/contact", params={"q": "@example.com"})).json()["items"]
    emails = [row["email"] for row in items]
    assert emails.index(sales["email"].lower()) < emails.index(plain["email"].lower())
    assert next(row for row in items if row["email"] == sales["email"].lower())["is_sales"] is True


async def test_taking_a_message_opens_it_and_marking_spam_hides_it(make_client, api_client):
    client, user = await _staff(make_client)
    body, made = await _message(api_client)
    message = await contact_messages.find_one({"reference": made["reference"]})
    taken = (await client.patch(f"{L}/contact/{message['id']}", json={"assign_to_me": True})).json()
    assert taken["status"] == "open" and taken["assignee"]["id"] == user["id"]
    await client.patch(f"{L}/contact/{message['id']}", json={"status": "spam"})
    hidden = (await client.get(f"{L}/contact", params={"q": made["reference"]})).json()
    assert hidden["total"] == 0
    shown = (await client.get(f"{L}/contact", params={"q": made["reference"], "status": "spam"})).json()
    assert shown["total"] == 1


async def test_a_reply_is_sent_recorded_and_marks_the_message_answered(make_client, api_client):
    client, _ = await _staff(make_client)
    _, made = await _message(api_client)
    message = await contact_messages.find_one({"reference": made["reference"]})
    res = await client.post(f"{L}/contact/{message['id']}/reply", json={"message": "Thank you, we can help with ten seats and more."})
    assert res.status_code == 200 and res.json()["status"] == "answered"
    detail = (await client.get(f"{L}/contact/{message['id']}")).json()
    assert detail["status"] == "answered" and detail["replies"][0]["text"].startswith("Thank you")


async def test_a_reply_that_cannot_be_sent_is_not_recorded(make_client, api_client, monkeypatch):
    import app.api.v1.ops_leads as module

    async def fail(*args, **kwargs):
        return False

    monkeypatch.setattr(module, "send_templated_email", fail)
    client, _ = await _staff(make_client)
    _, made = await _message(api_client)
    message = await contact_messages.find_one({"reference": made["reference"]})
    res = await client.post(f"{L}/contact/{message['id']}/reply", json={"message": "This will not go."})
    assert res.status_code == 502
    assert (await contact_messages.find_one({"id": message["id"]}))["replies"] == []


async def test_staff_can_email_a_lead_but_not_one_who_unsubscribed(make_client, api_client):
    client, _ = await _staff(make_client)
    _, lead = await _lead(api_client)
    sent = await client.post(f"{L}/waitlist/{lead['id']}/email", json={"subject": "A quick question", "message": "Which platforms do you post to most?"})
    assert sent.status_code == 200
    detail = (await client.get(f"{L}/waitlist/{lead['id']}")).json()
    assert detail["emails"][0]["subject"] == "A quick question"
    await waitlist_leads.update_one({"id": lead["id"]}, {"$set": {"status": "unsubscribed"}})
    blocked = await client.post(f"{L}/waitlist/{lead['id']}/email", json={"subject": "Hello", "message": "Are you there?"})
    assert blocked.status_code == 409


async def test_an_email_subject_cannot_hold_a_line_break(make_client, api_client):
    client, _ = await _staff(make_client)
    _, lead = await _lead(api_client)
    res = await client.post(f"{L}/waitlist/{lead['id']}/email", json={"subject": "Hi\nBcc: someone@example.com", "message": "Hello there."})
    assert res.status_code == 422


async def test_the_overview_counts_both_lists(make_client, api_client):
    client, _ = await _staff(make_client)
    await _lead(api_client, utm={"source": "newsletter"})
    await _message(api_client, topic="seats")
    data = (await client.get(f"{L}/overview")).json()
    assert data["waitlist"]["total"] >= 1 and data["waitlist"]["by_status"].get("joined", 0) >= 1
    assert data["contact"]["open_sales"] >= 1 and data["contact"]["total"] >= 1
