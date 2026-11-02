"""Deleting your own account: your workspaces and personal rows go, your place in someone else's workspace goes, their workspace and
its content stay, receipts stay, and Ekavo staff are refused."""
from __future__ import annotations

from uuid import uuid4

from app.db.mongo import content_pieces, get_db, users, workspace_members, workspaces
from tests.conftest import create_workspace


async def test_deleting_my_account_removes_my_data_and_leaves_others(signup_user):
    me, profile = await signup_user("Leaving")
    mine = await create_workspace(me, f"Mine {uuid4().hex[:5]}")
    await content_pieces.insert_one({"piece_id": "p-mine", "workspace_id": mine, "user_id": profile["id"]})
    await get_db()["member_voice_settings"].insert_one({"id": uuid4().hex, "workspace_id": mine, "user_id": profile["id"]})
    await get_db()["billing_invoices"].insert_one({"id": uuid4().hex, "workspace_id": mine, "customer": {"email": profile["email"]}})

    other, _ = await signup_user("Staying")
    theirs = await create_workspace(other, f"Theirs {uuid4().hex[:5]}")
    await content_pieces.insert_one({"piece_id": "p-theirs", "workspace_id": theirs, "user_id": profile["id"]})
    await workspace_members.insert_one({"id": uuid4().hex, "workspace_id": theirs, "user_id": profile["id"], "role": "editor", "status": "active"})

    assert (await me.request("DELETE", "/api/v1/users/me", json={"confirm": "nope"})).status_code == 400
    res = await me.request("DELETE", "/api/v1/users/me", json={"confirm": "DELETE"})
    assert res.status_code == 200, res.text

    assert await users.find_one({"id": profile["id"]}) is None
    assert await workspaces.find_one({"id": mine}) is None and await content_pieces.find_one({"piece_id": "p-mine"}) is None
    assert await get_db()["member_voice_settings"].count_documents({"user_id": profile["id"]}) == 0
    assert await workspace_members.count_documents({"user_id": profile["id"]}) == 0
    # someone else's workspace keeps what was made there; receipts are kept for the books
    assert await workspaces.find_one({"id": theirs}) and await content_pieces.find_one({"piece_id": "p-theirs"})
    assert await get_db()["billing_invoices"].count_documents({"workspace_id": mine}) == 1
    assert await get_db()["account_erasures"].count_documents({}) >= 1


async def test_staff_accounts_are_not_deleted_from_the_app(signup_user):
    me, profile = await signup_user("Staff")
    await users.update_one({"id": profile["id"]}, {"$set": {"is_platform_staff": True}})
    res = await me.request("DELETE", "/api/v1/users/me", json={"confirm": "DELETE"})
    assert res.status_code == 409 and await users.find_one({"id": profile["id"]})
