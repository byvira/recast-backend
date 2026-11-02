"""Device notifications: turning a device on and off, which notices reach whom, plain words for a lock screen, the kinds a member
chose, the hourly cap, devices the browser says are gone, and a device moving to whoever signs in on it. The push services themselves
are replaced by a stand-in, so nothing leaves the test."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.core.config import settings
from app.shared import push
from app.shared.activity.store import upsert_entry
from tests.test_payments import _owner_with_workspace


def _sub(n: str = "") -> dict:
    return {"endpoint": f"https://push.example.test/{uuid4().hex}{n}", "keys": {"p256dh": "p" * 40, "auth": "a" * 20}}


@pytest.fixture
def sent(monkeypatch):
    """Keys set, and every send recorded instead of made. A device whose address contains "gone" answers 410."""
    monkeypatch.setattr(settings, "VAPID_PUBLIC_KEY", "test-public-key")
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", "test-private-key")
    out: list[tuple[str, dict]] = []

    def fake(device, payload, urgency):
        out.append((device["endpoint"], payload))
        return 410 if "gone" in device["endpoint"] else None

    monkeypatch.setattr(push, "_send_one", fake)
    return out


def test_words_are_fit_for_a_lock_screen():
    assert push.clean("Post failed — try again", 60) == "Post failed, try again"
    assert push.clean("  Two   spaces here  ", 60) == "Two spaces here"
    long = push.clean("word " * 50, 30)
    assert len(long) <= 30 and long.endswith("…") and "  " not in long
    assert push.clean(None, 20) == ""


def test_only_notices_worth_interrupting_for_reach_a_member():
    me = "user-1"
    assert push.group_of({"lane": "active", "actor": {"user_id": "other"}}, me) == "needs_you"
    assert push.group_of({"status": "failed", "actor": {"type": "system_cron"}}, me) == "problems"
    assert push.group_of({"category": "content_generated", "actor": {"user_id": me}}, me) == "ready"
    assert push.group_of({"category": "post_published", "actor": {"type": "system_cron"}}, me) == "updates"
    # their own clicks and a teammate's routine edits are never pushed
    assert push.group_of({"category": "content_edited", "actor": {"user_id": me}}, me) is None
    assert push.group_of({"category": "content_edited", "actor": {"user_id": "other", "type": "member"}}, me) is None
    assert push.icon_for({"status": "failed"}, "problems").endswith("/failed.png")
    assert push.icon_for({"category": "credits"}, "updates").endswith("/credits.png")


async def test_turning_a_device_on_test_and_off(signup_user, sent):
    client, _profile = await signup_user("Pusher")
    cfg = (await client.get("/api/v1/push/config")).json()
    assert cfg["enabled"] is True and cfg["public_key"] and len(cfg["groups"]) == 4
    sub = _sub()
    assert (await client.post("/api/v1/push/devices", json={"subscription": sub, "label": "Chrome on Android"})).status_code == 200
    devices = (await client.get("/api/v1/push/devices")).json()["devices"]
    assert [d["label"] for d in devices] == ["Chrome on Android"] and "endpoint" not in devices[0]
    res = await client.post("/api/v1/push/test")
    assert res.status_code == 200 and res.json()["sent"] == 1
    title = sent[-1][1]["title"]
    assert title.startswith("You're all set") and "—" not in title
    assert (await client.post("/api/v1/push/devices/forget", json={"endpoint": sub["endpoint"]})).json()["removed"] == 1
    assert (await client.post("/api/v1/push/test")).status_code == 404


async def test_a_device_moves_to_whoever_signs_in_on_it(signup_user, sent):
    first, p1 = await signup_user("First")
    second, p2 = await signup_user("Second")
    sub = _sub()
    await first.post("/api/v1/push/devices", json={"subscription": sub, "label": "Shared phone"})
    await second.post("/api/v1/push/devices", json={"subscription": sub, "label": "Shared phone"})
    assert (await first.get("/api/v1/push/devices")).json()["devices"] == []
    assert len((await second.get("/api/v1/push/devices")).json()["devices"]) == 1


async def test_a_notice_reaches_members_devices_once_with_its_kind_and_choices(signup_user, sent):
    client, profile, ws_id = await _owner_with_workspace(signup_user)
    await client.post("/api/v1/push/devices", json={"subscription": _sub(), "label": "Phone"})
    entry = {
        "_id": f"system:test:{uuid4().hex}", "workspace_id": ws_id, "lane": "passive", "visibility": "workspace",
        "actor": {"type": "system_cron", "name": "Ekavo"}, "category": "post_published", "status": "success",
        "title": "Your post went out on LinkedIn — nice", "description": "It is live now.", "href": "/dashboard/library",
        "occurred_at": datetime.now(timezone.utc),
    }
    await push.notify_entry(entry)
    assert len(sent) == 1
    payload = sent[0][1]
    assert payload["title"] == "Your post went out on LinkedIn, nice" and payload["icon"].endswith("/published.png")
    assert payload["badge"].endswith("/badge.png") and payload["workspace_id"] == ws_id and payload["url"] == "/dashboard/library"

    # the member turned "updates" off: nothing more of that kind
    await client.put("/api/v1/push/prefs", json={"prefs": {"updates": False}})
    await push.notify_entry({**entry, "_id": f"system:test:{uuid4().hex}"})
    assert len(sent) == 1
    # work still going on is not news yet
    await push.notify_entry({**entry, "_id": f"system:test:{uuid4().hex}", "category": "content_generated", "status": "running"})
    assert len(sent) == 1
    # a failure still comes through
    await push.notify_entry({**entry, "_id": f"system:test:{uuid4().hex}", "status": "failed", "title": "Your post didn't go out"})
    assert len(sent) == 2 and sent[-1][1]["icon"].endswith("/failed.png")


async def test_saving_a_notice_sends_it_and_saving_it_again_does_not(signup_user, sent):
    import asyncio

    client, _, ws_id = await _owner_with_workspace(signup_user)
    await client.post("/api/v1/push/devices", json={"subscription": _sub(), "label": "Phone"})
    entry = {
        "_id": f"system:test:{uuid4().hex}", "workspace_id": ws_id, "lane": "passive", "visibility": "workspace",
        "actor": {"type": "system_cron", "name": "Ekavo"}, "category": "post_published", "status": "success",
        "title": "Published", "description": "Live now.", "occurred_at": datetime.now(timezone.utc),
    }
    await upsert_entry(entry)
    await asyncio.gather(*list(push._tasks))
    await upsert_entry(entry)  # the same notice again, same outcome
    await asyncio.gather(*list(push._tasks))
    assert len(sent) == 1


async def test_a_gone_device_is_removed_and_the_hourly_cap_holds(signup_user, sent, monkeypatch):
    client, profile = await signup_user("Busy")
    await client.post("/api/v1/push/devices", json={"subscription": _sub("gone"), "label": "Old phone"})
    assert await push.send_to_user(profile["id"], {"title": "x"}) == 0
    assert (await client.get("/api/v1/push/devices")).json()["devices"] == []
    monkeypatch.setattr(push, "HOURLY_CAP", 2)
    assert [await push._under_cap(profile["id"]) for _ in range(3)] == [True, True, False]


async def test_a_member_never_sees_or_removes_another_members_device(signup_user, sent):
    first, _ = await signup_user("Owner of phone")
    other, _ = await signup_user("Someone else")
    await first.post("/api/v1/push/devices", json={"subscription": _sub(), "label": "Phone"})
    device_id = (await first.get("/api/v1/push/devices")).json()["devices"][0]["id"]
    assert (await other.delete(f"/api/v1/push/devices/{device_id}")).status_code == 404
    assert len((await first.get("/api/v1/push/devices")).json()["devices"]) == 1


async def test_without_keys_nothing_is_sent_and_turning_on_says_so(signup_user, monkeypatch):
    monkeypatch.setattr(settings, "VAPID_PUBLIC_KEY", "")
    monkeypatch.setattr(settings, "VAPID_PRIVATE_KEY", "")
    client, _ = await signup_user("No keys")
    assert (await client.get("/api/v1/push/config")).json()["enabled"] is False
    assert (await client.post("/api/v1/push/devices", json={"subscription": _sub(), "label": "Phone"})).status_code == 503
