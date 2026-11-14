"""Reminders (L27), free-first: off for everyone until a person sends REMIND ON, one a day, never in quiet hours, a pointer and a link only, WhatsApp template messages (charged by
Meta) capped at zero until Ops raises the caps, free messages inside the 24-hour window, a failed send never hammered, and the spend visible to Ops. No network is called."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from app.channels import engine, link as links, reminders
from app.channels.base import Inbound
from app.channels.simulator import SimulatorAdapter
from app.core import staff
from app.core.config import settings
from app.db.mongo import get_db, users
from tests.conftest import create_workspace, signup_new_user

NOON = datetime(2026, 11, 13, 6, 30, tzinfo=timezone.utc)            # noon in India
NIGHT = datetime(2026, 11, 13, 18, 0, tzinfo=timezone.utc)           # 23:30 in India


class Rec(SimulatorAdapter):
    """Records free messages and template messages separately."""

    def __init__(self, ok=True):
        super().__init__()
        self.templates, self.ok = [], ok

    async def send(self, to, text, buttons=None):
        await super().send(to, text, buttons)
        return self.ok

    async def send_template(self, to, name, params, language="en"):
        self.templates.append((to, name, params))
        return self.ok


@pytest.fixture
async def world(signup_user, monkeypatch):
    client, profile = await signup_user()
    ws = await create_workspace(client, f"Remind {uuid4().hex[:6]}")
    await engine.create_indexes()
    await reminders.create_indexes()
    for name in ("channel_seen", "channel_hints", "channel_links", "channel_codes", "channel_attempts", "channel_reminders", "channel_settings"):
        await get_db()[name].delete_many({})
    monkeypatch.setattr(settings, "FERNET_SECRET_KEY", Fernet.generate_key().decode())
    import app.pipelines.publish.token_store as ts

    monkeypatch.setattr(ts, "_fernet", None)
    return client, profile["id"], ws


async def link_chat(ws, user, channel="telegram", sender="555111222", opt_in=True, last_inbound=None):
    made = await links.make_code(ws, user, "simulator")
    row = await links.redeem("simulator", sender, made["code"], __import__("app.tools.services", fromlist=["LiveServices"]).LiveServices())
    await links.links().update_one({"id": row["id"]}, {"$set": {"channel": channel, "sender_hash": links.sender_hash(channel, sender), "last_inbound_at": last_inbound or datetime.now(timezone.utc)}})
    if opt_in:
        await reminders.opt_in({**row, "channel": channel}, sender)
    return row["id"]


def test_the_words_the_quiet_hours_and_the_window_are_read_correctly():
    assert reminders.parse_word(" Remind ON ") is True and reminders.parse_word("reminders off") is False and reminders.parse_word("remind me later") is None
    assert not reminders.is_quiet(NOON) and reminders.is_quiet(NIGHT) and reminders.is_quiet(datetime(2026, 11, 13, 1, 0, tzinfo=timezone.utc))           # 06:30 in India
    assert reminders.window_open({"last_inbound_at": NOON - timedelta(hours=23)}, NOON) and not reminders.window_open({"last_inbound_at": NOON - timedelta(hours=25)}, NOON)
    assert not reminders.window_open({}, NOON)


async def test_remind_on_keeps_the_chat_id_encrypted_and_remind_off_deletes_it(world):
    client, user, ws = world
    adapter = SimulatorAdapter()
    made = await links.make_code(ws, user, "simulator")
    await engine.handle(adapter, Inbound(channel="simulator", sender="919990009999", message_id="a", text=made["message"]), wait=True)
    assert await engine.handle(adapter, Inbound(channel="simulator", sender="919990009999", message_id="b", text="REMIND ON"), wait=True) == "reminders_on"
    row = await links.links().find_one({"workspace_id": ws})
    assert row["reminders"] is True and "919990009999" not in str(row.get("sender_enc")) and "At most one message a day" in adapter.last["text"]
    assert await engine.handle(adapter, Inbound(channel="simulator", sender="919990009999", message_id="c", text="remind off"), wait=True) == "reminders_off"
    row = await links.links().find_one({"workspace_id": ws})
    assert row["reminders"] is False and "sender_enc" not in row


async def test_without_encryption_set_up_nothing_is_kept_and_the_person_is_told(world, monkeypatch):
    client, user, ws = world
    monkeypatch.setattr(settings, "FERNET_SECRET_KEY", "")
    adapter = SimulatorAdapter()
    made = await links.make_code(ws, user, "simulator")
    await engine.handle(adapter, Inbound(channel="simulator", sender="919990008888", message_id="a", text=made["message"]), wait=True)
    assert await engine.handle(adapter, Inbound(channel="simulator", sender="919990008888", message_id="b", text="REMIND ON"), wait=True) == "reminders_unavailable"
    assert "sender_enc" not in (await links.links().find_one({"workspace_id": ws}))


async def test_nobody_is_reminded_until_they_opt_in_and_a_pointer_is_free_on_telegram(world):
    client, user, ws = world
    await link_chat(ws, user, opt_in=False)
    tg = Rec()
    assert await reminders.nudge(ws, user, 3, {"telegram": tg}, NOON) == []                              # nobody opted in
    await get_db()["channel_links"].delete_many({})
    await link_chat(ws, user)
    out = await reminders.nudge(ws, user, 3, {"telegram": tg}, NOON)
    assert [o["result"] for o in out] == ["free"] and "3 drafts are waiting for your yes" in tg.sent[0]["text"] and tg.sent[0]["to"] == "555111222"
    assert "/dashboard" in tg.sent[0]["text"]                                                              # a pointer and a link, never a draft's text


async def test_one_a_day_never_at_night_and_the_daily_cap_for_a_workspace(world):
    client, user, ws = world
    await link_chat(ws, user)
    tg = Rec()
    assert [o["result"] for o in await reminders.nudge(ws, user, 1, {"telegram": tg}, NIGHT)] == ["quiet_hours"] and tg.sent == []
    assert [o["result"] for o in await reminders.nudge(ws, user, 1, {"telegram": tg}, NOON)] == ["free"]
    assert [o["result"] for o in await reminders.nudge(ws, user, 1, {"telegram": tg}, NOON)] == ["already_today"] and len(tg.sent) == 1
    await reminders.set_limits({"telegram_per_day": 0})
    await reminders.log().delete_many({})
    assert [o["result"] for o in await reminders.nudge(ws, user, 1, {"telegram": tg}, NOON)] == ["daily_cap"]


async def test_whatsapp_inside_the_window_is_free_and_outside_it_needs_an_approved_template_and_a_cap_above_zero(world, monkeypatch):
    client, user, ws = world
    wa = Rec()
    await link_chat(ws, user, "whatsapp", last_inbound=NOON - timedelta(hours=2))
    assert reminders.DEFAULTS["whatsapp_per_day"] == 0
    assert [o["result"] for o in await reminders.nudge(ws, user, 2, {"whatsapp": wa}, NOON)] == ["daily_cap"]            # off until Ops raises it
    await reminders.set_limits({"whatsapp_per_day": 5})
    assert [o["result"] for o in await reminders.nudge(ws, user, 2, {"whatsapp": wa}, NOON)] == ["free"] and wa.templates == []         # inside the window: a free message
    await get_db()["channel_reminders"].delete_many({})
    await links.links().update_many({}, {"$set": {"last_inbound_at": NOON - timedelta(hours=30)}})
    assert [o["result"] for o in await reminders.nudge(ws, user, 2, {"whatsapp": wa}, NOON)] == ["no_template"]                          # no approved template set
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_DRAFTS", "ekavo_drafts_waiting")
    assert [o["result"] for o in await reminders.nudge(ws, user, 2, {"whatsapp": wa}, NOON)] == ["monthly_cap"]                          # the monthly cap is still zero
    await reminders.set_limits({"whatsapp_per_month": 10})
    assert [o["result"] for o in await reminders.nudge(ws, user, 2, {"whatsapp": wa}, NOON)] == ["template"]
    assert wa.templates == [("555111222", "ekavo_drafts_waiting", ["2"])]
    spent = await reminders.spend(NOON.replace(day=1, hour=0))
    assert spent["templates"] == 1 and spent["estimated_rupees"] == 0.15


async def test_a_failed_send_is_logged_and_counts_so_a_broken_connection_is_not_hammered(world):
    client, user, ws = world
    await link_chat(ws, user)
    tg = Rec(ok=False)
    assert [o["result"] for o in await reminders.nudge(ws, user, 1, {"telegram": tg}, NOON)] == ["send_failed"]
    assert [o["result"] for o in await reminders.nudge(ws, user, 1, {"telegram": tg}, NOON)] == ["already_today"] and len(tg.sent) == 1


async def test_ops_sets_the_caps_and_sees_the_spend(world, make_client):
    client, user, ws = world
    admin = make_client()
    staff_user = await signup_new_user(admin, name="Staff")
    await users.update_one({"id": staff_user["id"]}, {"$set": {"is_platform_staff": True, "is_master_admin": True, "staff": {"level": "admin"}}})
    staff.forget(staff_user["id"])
    assert (await client.put("/api/v1/ops/channels/reminders", json={"whatsapp_per_day": 5})).status_code in (401, 403)
    res = await admin.put("/api/v1/ops/channels/reminders", json={"whatsapp_per_day": 5, "whatsapp_per_month": 100, "reason": "try templates"})
    assert res.status_code == 200 and res.json()["limits"]["whatsapp_per_day"] == 5 and res.json()["limits"]["telegram_per_day"] == 3
    assert (await admin.put("/api/v1/ops/channels/reminders", json={"whatsapp_per_day": -1})).status_code == 422
    page = (await admin.get("/api/v1/ops/channels")).json()
    assert page["reminders"]["limits"]["whatsapp_per_month"] == 100 and page["reminders"]["spend"]["templates"] == 0
