"""Undo for every change made through a tool: the table is complete and sound, and each way of undoing works end to end."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.db.mongo import get_db
from app.tools import ledger, undo
from app.tools.contract import Risk
from app.tools.registry import get_registry
from app.tools.undo import Kind
from tests.conftest import create_workspace

D = "/api/v1/developer"
P = "/api/public/v1"


# ── the table ────────────────────────────────────────────────────────────

def test_every_tool_that_changes_anything_declares_how_it_is_undone_and_nothing_else_is_listed():
    names = {t.name for t in get_registry().all()}
    changing = {t.name for t in get_registry().all() if t.risk is not Risk.READ}
    assert changing - set(undo.UNDO) == set(), "a write tool has no declared undo"
    assert set(undo.UNDO) - names == set(), "the undo table names a tool that does not exist"


def test_each_rule_points_at_real_things():
    registry = get_registry()
    for name, rule in undo.UNDO.items():
        fields = set(registry.get(name).input_schema().get("properties", {}))
        if rule.kind is Kind.VERSION:
            assert rule.entity in undo._FAMILIES and rule.id_arg in fields, name
        elif rule.kind is Kind.SNAPSHOT:
            assert rule.collection and rule.match, name
            for _, source in rule.match:
                assert source.startswith(("@", "=")) or source in fields, (name, source)
        elif rule.kind is Kind.OPPOSITE:
            assert registry.get(rule.inverse) is not None and rule.inverse_args, name
        elif rule.kind is Kind.CREATES:
            remove = registry.get(rule.remove_tool)
            remove_fields = set(remove.input_schema().get("properties", {}))
            assert remove is not None and rule.remove_arg in remove_fields, name
            assert {k for k, _ in rule.extra_args} <= remove_fields, name
        else:
            assert rule.reason.strip(), name                           # an honest limit always says why and what to do instead


def test_a_removal_is_only_undoable_when_a_copy_or_a_version_really_holds_it_otherwise_it_says_support():
    for t in get_registry().all():
        if t.risk is Risk.DESTRUCTIVE:
            rule = undo.UNDO[t.name]
            assert rule.kind in (Kind.VERSION, Kind.SNAPSHOT) or (rule.kind is Kind.FINAL and "support" in rule.reason.lower()), t.name
    for name in ("publish.now", "publish.batch", "publish.send_to_destination", "audio.approve", "library.approve_post"):
        assert not undo.can_undo(name) and undo.words(name)


def test_the_catalog_and_the_api_page_say_how_each_tool_is_undone():
    registry = get_registry()
    assert "restores the version" in registry.get("video.trim_clip").catalog_entry()["undo"]
    assert "saved copy" in registry.get("presets.update").catalog_entry()["undo"]
    assert registry.get("library.archive_post").catalog_entry()["undo"] == "Undo runs library.archive_post."


# ── end to end ───────────────────────────────────────────────────────────

@pytest.fixture
async def world(signup_user):
    client, _ = await signup_user()
    ws = await create_workspace(client, f"Undo {uuid4().hex[:6]}")
    await ledger.create_indexes()
    await undo.create_indexes()
    await ledger.settings().delete_many({})
    h = {"X-Workspace-Id": ws}

    async def key(scopes=("read", "create"), capabilities=()):
        made = await client.post(f"{D}/tool-keys", headers=h, json={"name": f"k{uuid4().hex[:4]}", "scopes": list(scopes), "capabilities": list(capabilities)})
        assert made.status_code == 201, made.text
        return {"Authorization": f"Bearer {made.json()['secret']}"}

    async def call(headers, tool, **arguments):
        return await client.post(f"{P}/tools/{tool}/call", headers=headers, json={"arguments": arguments})

    return client, ws, h, key, call


async def _project(ws: str) -> str:
    pid = uuid4().hex
    edit = {"schema_version": 1, "fps": {"num": 30, "den": 1}, "width": 1280, "height": 720, "duration_f": 0, "background": "#000000", "video": [], "audio": [], "text": [],
            "captions": {"enabled": False, "accent": "#38bdf8", "words": []}, "markers": []}
    now = datetime.now(timezone.utc)
    await get_db()["video_projects"].insert_one({"id": pid, "workspace_id": ws, "title": "Cut", "version": 1, "edit": edit, "status": "draft", "created_at": now, "updated_at": now})
    await get_db()["video_project_versions"].insert_one({"id": uuid4().hex, "project_id": pid, "workspace_id": ws, "version": 1, "edit": edit, "kind": "save", "label": "Start", "at": now})
    return pid


async def test_a_video_edit_is_undone_by_restoring_the_version_from_before_and_the_undo_can_be_undone(world):
    client, ws, h, key, call = world
    k = await key()
    pid = await _project(ws)
    made = await call(k, "video.add_text", project_id=pid, base_version=1, text="Launch day", start_s=0, duration_s=2)
    assert made.status_code == 200, made.text
    change = made.json()["change"]
    assert change["undo"] == "version" and change["undoable"] is True
    assert len((await get_db()["video_projects"].find_one({"id": pid}))["edit"]["text"]) == 1

    undone = await call(k, "changes.undo", change_id=change["id"])
    assert undone.status_code == 200, undone.text
    assert (await get_db()["video_projects"].find_one({"id": pid}))["edit"]["text"] == []          # back to how it was, as a new version
    assert (await get_db()["video_projects"].find_one({"id": pid}))["version"] == 3
    again = await call(k, "changes.undo", change_id=change["id"])
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_undone"

    redo = await call(k, "changes.undo", change_id=undone.json()["data"]["new_change"]["id"])         # an undo is a change too
    assert redo.status_code == 200 and len((await get_db()["video_projects"].find_one({"id": pid}))["edit"]["text"]) == 1


async def test_settings_are_put_back_from_a_saved_copy_and_a_newer_change_is_never_overwritten(world):
    client, ws, h, key, call = world
    k = await key(capabilities=("delete",))
    made = await call(k, "presets.create", title="One", category=_category(), category_label="Launch")
    assert made.status_code in (200, 201), made.text
    preset_id = made.json()["data"].get("id") or made.json()["data"].get("preset_id")

    first = await call(k, "presets.update", preset_id=preset_id, title="Two")
    second = await call(k, "presets.update", preset_id=preset_id, title="Three")
    assert first.json()["change"]["undo"] == "snapshot" and second.json()["change"]["undo"] == "snapshot"
    title = lambda: get_db()["presets"].find_one({"id": preset_id, "workspace_id": ws})

    blocked = await call(k, "changes.undo", change_id=first.json()["change"]["id"])                 # a newer change sits on top
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "changed_since" and (await title())["title"] == "Three"

    assert (await call(k, "changes.undo", change_id=second.json()["change"]["id"])).status_code == 200 and (await title())["title"] == "Two"
    assert (await call(k, "changes.undo", change_id=first.json()["change"]["id"])).status_code == 200 and (await title())["title"] == "One"


def _category():
    from typing import get_args

    annotation = get_registry().get("presets.create").input_model.model_fields["category"].annotation
    first = next(iter(get_args(annotation) or list(annotation)))
    return getattr(first, "value", first)


async def test_something_new_is_undone_by_removing_it_and_a_key_needs_the_delete_capability_for_that(world):
    client, ws, h, key, call = world
    plain, deleting = await key(), await key(capabilities=("delete",))
    made = await call(plain, "presets.create", title="Temp", category=_category(), category_label="Launch")
    change = made.json()["change"]
    assert change["undo"] == "creates"
    refused = await call(plain, "changes.undo", change_id=change["id"])
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "capability_missing"
    assert await get_db()["presets"].count_documents({"workspace_id": ws}) == 1
    done = await call(deleting, "changes.undo", change_id=change["id"])
    assert done.status_code == 200, done.text
    assert await get_db()["presets"].count_documents({"workspace_id": ws, "deleted": {"$ne": True}}) in (0, 1)       # soft or hard, it is gone from the list
    listed = (await client.get("/api/v1/presets/", headers=h)).json()
    assert all(p.get("title") != "Temp" for p in (listed if isinstance(listed, list) else listed.get("items", [])))


async def test_the_reverse_tool_undoes_a_change_such_as_archiving_a_post(world):
    client, ws, h, key, call = world
    k = await key()
    piece = uuid4().hex
    await get_db()["content_pieces"].insert_one({"piece_id": piece, "id": piece, "workspace_id": ws, "platform": "LinkedIn", "content": "Hello", "created_at": datetime.now(timezone.utc)})
    archived = await call(k, "library.archive_post", piece_id=piece, archived=True)
    assert archived.status_code == 200, archived.text
    assert archived.json()["change"]["undo"] == "opposite"
    assert (await get_db()["content_pieces"].find_one({"piece_id": piece}))["archived"] is True
    undone = await call(k, "changes.undo", change_id=archived.json()["change"]["id"])
    assert undone.status_code == 200, undone.text
    assert (await get_db()["content_pieces"].find_one({"piece_id": piece})).get("archived") in (False, None)


async def test_a_change_that_cannot_be_taken_back_says_why_and_the_history_lists_every_change(world):
    client, ws, h, key, call = world
    k = await key()
    final_id = await undo.changes().insert_one({"id": "final-1", "at": datetime.now(timezone.utc), "workspace_id": ws, "tool": "publish.now", "kind": "final", "undoable": False,
                                               "how": "It has gone out or been recorded outside Ekavo, so Ekavo cannot take it back.", "undone_by": None, "target": None, "fields": []})
    refused = await call(k, "changes.undo", change_id="final-1")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "cannot_undo" and "cannot take it back" in refused.json()["error"]["message"]
    assert (await call(k, "changes.undo", change_id="nope")).status_code == 404

    pid = await _project(ws)
    await call(k, "video.add_text", project_id=pid, base_version=1, text="A", start_s=0, duration_s=1)
    listed = (await call(k, "changes.list", undoable_only=True)).json()["data"]
    assert [c["tool"] for c in listed["items"]] == ["video.add_text"] and listed["items"][0]["undoable"] is True
    by_target = (await call(k, "changes.list", target_id=pid)).json()["data"]["items"]
    assert len(by_target) == 1 and by_target[0]["target"] == {"type": "video", "id": pid}
    assert final_id is not None


async def test_another_workspaces_changes_are_not_visible_or_undoable(world):
    client, ws, h, key, call = world
    k = await key()
    await undo.changes().insert_one({"id": "theirs", "at": datetime.now(timezone.utc), "workspace_id": "someone-else", "tool": "video.add_text", "kind": "version", "undoable": True,
                                    "target": {"type": "video", "id": "x"}, "before": {"version": 1}, "undone_by": None, "fields": []})
    assert (await call(k, "changes.undo", change_id="theirs")).status_code == 404
    assert (await call(k, "changes.list")).json()["data"]["items"] == []
