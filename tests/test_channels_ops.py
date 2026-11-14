"""Ops for chat apps: who may see and change them, the per-channel switch acting on the engine at once, usage, and the abuse watch."""
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.channels import engine, link as links
from app.channels.base import Inbound
from app.channels.simulator import SimulatorAdapter
from app.core import staff
from app.db.mongo import get_db, users
from app.tools import ledger
from tests.conftest import create_workspace, signup_new_user

O = "/api/v1/ops/channels"


async def _staff(make_client):
    client = make_client()
    user = await signup_new_user(client, name="Staff")
    await users.update_one({"id": user["id"]}, {"$set": {"is_platform_staff": True, "is_master_admin": True, "staff": {"level": "admin"}}})
    staff.forget(user["id"])
    return client


@pytest.fixture
async def world(signup_user, make_client):
    member, _ = await signup_user()
    ws = await create_workspace(member, f"ChOps {uuid4().hex[:6]}")
    await engine.create_indexes()
    await ledger.create_indexes()
    await ledger.settings().delete_many({})
    for name in ("channel_seen", "channel_hints", "channel_links", "channel_codes", "channel_attempts"):
        await get_db()[name].delete_many({})
    return member, ws, await _staff(make_client)


async def test_only_platform_staff_can_see_or_change_chat_apps_here(world):
    member, ws, admin = world
    assert (await member.get(O)).status_code in (401, 403)
    assert (await admin.get(O)).status_code == 200
    assert (await member.put(f"{O}/switch", json={"channel": "telegram", "off": True})).status_code in (401, 403)
    assert (await admin.put(f"{O}/switch", json={"channel": "email", "off": True})).status_code == 422


async def test_the_switch_is_audited_and_stops_that_channel_on_the_very_next_message(world):
    member, ws, admin = world
    from app.tools.services import LiveServices

    res = await admin.put(f"{O}/switch", json={"channel": "telegram", "off": True, "message": "Paused for now.", "reason": "testing"})
    assert res.status_code == 200
    channels = {c["channel"]: c for c in res.json()["channels"]}
    assert channels["telegram"]["off"] is True and channels["whatsapp"]["off"] is False
    assert await LiveServices().switched_off(ws, "channel.telegram") == "Paused for now." and await LiveServices().switched_off(ws, "channel.whatsapp") is None
    await admin.put(f"{O}/switch", json={"channel": "telegram", "off": False, "reason": "done"})
    assert await LiveServices().switched_off(ws, "channel.telegram") is None


async def test_usage_counts_linked_chats_messages_and_active_chats(world):
    member, ws, admin = world
    now = datetime.now(timezone.utc)
    await links.links().insert_one({"id": "l1", "channel": "whatsapp", "sender_hash": "h1", "sender_tail": "4821", "workspace_id": ws, "user_id": "u", "status": "active", "created_at": now})
    await engine.seen().insert_many([{"channel": "whatsapp", "message_id": f"m{i}", "sender_hash": "h1", "at": now} for i in range(3)]
                                    + [{"channel": "whatsapp", "message_id": "m9", "sender_hash": "h2", "at": now}])
    page = (await admin.get(O)).json()
    wa = next(c for c in page["channels"] if c["channel"] == "whatsapp")
    assert wa["linked"] == 1 and wa["messages"] == 4 and wa["active_chats"] == 2
    tg = next(c for c in page["channels"] if c["channel"] == "telegram")
    assert tg["linked"] == 0 and tg["messages"] == 0 and tg["configured"] is False


async def test_the_abuse_watch_flags_a_flood_by_the_last_four_characters_and_many_wrong_codes(world):
    member, ws, admin = world
    now = datetime.now(timezone.utc)
    await links.links().insert_one({"id": "l2", "channel": "telegram", "sender_hash": "flood", "sender_tail": "9911", "workspace_id": ws, "user_id": "u", "status": "active", "created_at": now})
    await engine.seen().insert_many([{"channel": "telegram", "message_id": f"f{i}", "sender_hash": "flood", "at": now} for i in range(engine.PER_HOUR + 2)])
    await links.attempts().insert_many([{"key": "guesser", "at": now} for _ in range(links.MAX_WRONG_CODES_PER_HOUR)])
    flags = (await admin.get(O)).json()["abuse"]
    flood = next(f for f in flags if f["chat"] == "9911")
    assert flood["channel"] == "telegram" and flood["count"] == engine.PER_HOUR + 2 and flood["workspace_id"] == ws
    assert any(f["why"].startswith("Tried many wrong link codes") for f in flags)
    assert "flood" not in str(flags)                                       # only the last four characters are ever shown
