"""Files as sources: Excel, and pictures, and how a file source is made, drafted from a few rows at a time and finished. The vision model, the content check and the tools are stand-ins."""
import io
import zipfile

import pytest

from app.db.mongo import get_db
from app.sources import documents, flow, pictures, service
from app.sources.items import Item, ReadError
from tests.test_sources_service import Tools, _everything, world  # noqa: F401  (world is a fixture)


def _workbook(rows, extra_sheet=None) -> bytes:
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    for row in rows:
        sheet.append(row)
    if extra_sheet:
        other = book.create_sheet("Second")
        for row in extra_sheet:
            other.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


PRODUCTS = [["Product", "Price", "Notes"], ["Filter coffee", 120.0, "Fresh every morning"], ["Cold brew", 180, "Slow steeped"], ["Masala chai", 60.5, "Cardamom and ginger"]]


def test_an_excel_sheet_gives_one_item_per_row_from_the_first_sheet_only():
    data = _workbook(PRODUCTS, extra_sheet=[["Other"], ["Ignored"]])
    assert documents.sniff(data, "menu.xlsx") == documents.XLSX
    header, items = documents.read_xlsx(data)
    assert header == ["Product", "Price", "Notes"] and [i.title for i in items] == ["Filter coffee", "Cold brew", "Masala chai"]
    assert "Price: 120" in items[0].text and "Price: 60.5" in items[2].text and "Ignored" not in " ".join(i.text for i in items)   # whole numbers lose their ".0"
    chosen = documents.read_xlsx(data, title_column="Notes", text_columns=["Product"])[1]
    assert chosen[0].title == "Fresh every morning" and chosen[0].text == "Product: Filter coffee"
    title, via_read = documents.read("Menu.xlsx", data)
    assert title == "Menu" and len(via_read) == 3 and documents.columns_of("Menu.xlsx", data) == header


def test_formulas_are_values_only_and_a_table_with_too_many_rows_or_columns_is_refused():
    from openpyxl import Workbook

    book = Workbook()
    book.active.append(["Name", "Total"])
    book.active.append(["Sum", "=1+1"])
    out = io.BytesIO()
    book.save(out)
    item = documents.read_xlsx(out.getvalue())[1][0]
    assert "=1+1" not in item.text and "2" not in item.text.replace("Total:", "")                   # a formula that was never calculated is an empty value, never run
    with pytest.raises(ReadError) as many:
        documents.read_xlsx(_workbook([["A", "B"]] + [[f"r{i}", "x"] for i in range(documents.MAX_ROWS + 5)]))
    assert many.value.code == "too_many_rows"
    with pytest.raises(ReadError) as wide:
        documents.read_xlsx(_workbook([[f"c{i}" for i in range(documents.MAX_COLUMNS + 3)], ["x"] * (documents.MAX_COLUMNS + 3)]))
    assert wide.value.code == "too_wide"


def test_a_workbook_that_unpacks_to_something_huge_a_damaged_one_and_a_renamed_program_are_refused():
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/workbook.xml", "<x/>")
        z.writestr("xl/big.bin", b"\0" * (documents.MAX_UNPACKED + 1024 * 1024))
    with pytest.raises(ReadError) as caught:
        documents.read("big.xlsx", bomb.getvalue())
    assert caught.value.code == "too_large" and "too large" in caught.value.message
    broken = _workbook(PRODUCTS)[:300]
    with pytest.raises(ReadError):
        documents.read("broken.xlsx", broken)
    with pytest.raises(ReadError) as program:
        documents.read("menu.xlsx", b"MZ\x90\x00" + b"\0" * 200)
    assert program.value.code == "unsupported"


async def _read(data, **over):
    seen = {}

    async def describe(prompt, blob, mime):
        seen.update(prompt=prompt, mime=mime)
        return over.get("answer", "TITLE: Weekend offer\nTEXT: Buy one get one free on all cold brews, Saturday and Sunday only. A poster with a coffee cup.")

    async def screen(blob, mime, ws):
        if over.get("refuse"):
            from fastapi import HTTPException

            raise HTTPException(status_code=422, detail=over["refuse"])               # the content check refuses with a plain message of its own

    out = await pictures.read_picture(data, over.get("name", "poster.jpg"), "w1", describe=over.get("describe", describe), screen=screen)
    return out, seen


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


async def test_a_picture_is_checked_then_read_into_one_item_and_people_are_never_named():
    (title, items), seen = await _read(JPEG)
    assert title == "Weekend offer" and len(items) == 1 and items[0].kind == "image" and "Buy one get one free" in items[0].text
    assert seen["mime"] == "image/jpeg" and "Never say who they are" in seen["prompt"]
    (named, plain_items), _ = await _read(JPEG, answer="Cold brew poster with the price 180 written on it")  # an answer without the labels is still used, named after the file
    assert named == "poster" and "price 180" in plain_items[0].text


async def test_a_picture_that_is_refused_too_big_the_wrong_type_empty_or_when_the_reader_is_down_gives_a_plain_message():
    with pytest.raises(ReadError) as refused:
        await _read(JPEG, refuse="That picture is not allowed.")
    assert refused.value.code == "picture_refused" and "not allowed" in refused.value.message
    with pytest.raises(ReadError) as big:
        await _read(JPEG + b"\0" * pictures.MAX_PICTURE_BYTES)
    assert big.value.code == "too_large"
    with pytest.raises(ReadError) as kind:
        await _read(b"GIF89a" + b"\0" * 40)
    assert kind.value.code == "unsupported"
    with pytest.raises(ReadError) as empty:
        await _read(JPEG, answer="TITLE: Nothing to read\nTEXT: empty")
    assert empty.value.code == "nothing_to_read"

    async def down(prompt, blob, mime):
        raise RuntimeError("provider down")

    with pytest.raises(ReadError) as unavailable:
        await _read(JPEG, describe=down)
    assert unavailable.value.code == "vision_unavailable" and "Try again" in unavailable.value.message


def _form(brand, **over):
    return {"brand_id": brand, "platforms": "LinkedIn", "relevance": "everything", **over}


async def _add(client, h, brand, data, name="menu.xlsx", **over):
    return await client.post("/api/v1/sources/file", headers=h, data=_form(brand, **over), files={"file": (name, data, "application/octet-stream")})


async def test_a_file_is_previewed_then_followed_and_its_rows_are_drafted_a_few_at_a_time_until_it_finishes(world):
    client, user, ws, brand, h = world
    rows = [["Product", "Notes"]] + [[f"Item {n}", f"About item {n}"] for n in range(1, 8)]
    data = _workbook(rows)
    peek = await client.post("/api/v1/sources/file/check", headers=h, files={"file": ("menu.xlsx", data, "application/octet-stream")})
    assert peek.status_code == 200 and peek.json()["count"] == 7 and peek.json()["columns"] == ["Product", "Notes"] and len(peek.json()["first"]) == 3
    assert (await client.get("/api/v1/sources", headers=h)).json()["items"] == []                              # looking made nothing

    made = await _add(client, h, brand, data, title_column="Product", text_columns="Notes")
    assert made.status_code == 201, made.text
    source = made.json()
    assert source["kind"] == "file" and source["file_kind"] == "xlsx" and source["file_rows"] == 7 and source["name"] == "menu"
    assert await service.pending_col().count_documents({"source_id": source["id"]}) == 7

    tools = Tools()
    stored = await get_db()["sources"].find_one({"id": source["id"]})
    first = await service.run(stored, force=True, tools=tools, score=_everything)
    assert first["ok"] and first["drafted"] == flow.MAX_PER_PASS
    assert [c[0] for c in tools.calls] == ["library.create_draft"] * flow.MAX_PER_PASS                          # drafts only
    assert await service.pending_col().count_documents({"source_id": source["id"]}) == 2
    assert (await get_db()["sources"].find_one({"id": source["id"]}))["status"] == "active"

    second = await service.run(await get_db()["sources"].find_one({"id": source["id"]}), force=True, tools=tools, score=_everything)
    assert second["drafted"] == 2 and len(tools.calls) == 7
    done = await get_db()["sources"].find_one({"id": source["id"]})
    assert done["status"] == "paused" and "Finished" in done["paused_reason"] and await service.pending_col().count_documents({"source_id": source["id"]}) == 0


async def test_the_same_file_twice_is_refused_and_the_same_rows_in_another_file_are_not_repeats(world):
    client, user, ws, brand, h = world
    first = _workbook([["Product", "Notes"], ["Tea", "Hot"]])
    assert (await _add(client, h, brand, first)).status_code == 201
    again = await _add(client, h, brand, first)
    assert again.status_code == 409 and "already added" in again.text
    other = _workbook([["Product", "Notes"], ["Tea", "Hot"], ["Cake", "Sweet"]])
    made = await _add(client, h, brand, other, name="menu2.xlsx")
    assert made.status_code == 201
    tools = Tools()
    results = [await service.run(s, force=True, tools=tools, score=_everything) for s in await get_db()["sources"].find({"workspace_id": ws}).to_list(None)]
    assert sum(r["drafted"] for r in results) == 3 and not any(r["skipped"].get("duplicate") for r in results)  # "Tea" is drafted once per file


async def test_a_file_source_needs_a_known_brand_and_platform_and_refuses_files_it_cannot_read(world):
    client, user, ws, brand, h = world
    data = _workbook(PRODUCTS)
    assert (await _add(client, h, "no-such-brand", data)).status_code == 422
    assert (await _add(client, h, brand, data, platforms=" , ")).status_code == 422
    program = await _add(client, h, brand, b"MZ\x90\x00" + b"\0" * 200, name="menu.xlsx")
    assert program.status_code == 422 and "cannot be read" in program.text
    assert (await client.get("/api/v1/sources", headers=h)).json()["items"] == []
    over = await _add(client, h, brand, b"x" * (10 * 1024 * 1024 + 5), name="notes.txt")
    assert over.status_code == 413


async def test_pictures_are_limited_each_day_and_a_picture_that_cannot_be_read_uses_none_of_the_allowance(world, monkeypatch):
    client, user, ws, brand, h = world
    monkeypatch.setattr(pictures, "DAILY_PICTURES", 3)                                                            # a small number, so the test stays under the route's own hourly limit
    calls = []

    async def fake(data, filename, workspace_id, **kw):
        calls.append(filename)
        if filename.startswith("bad"):
            raise ReadError("nothing_to_read", "Nothing could be read in that picture.")
        return "Offer", [Item(id=f"p{len(calls)}", title="Offer", text="Two for one", kind="image")]

    monkeypatch.setattr(pictures, "read_picture", fake)
    peek = await client.post("/api/v1/sources/file/check", headers=h, files={"file": ("a.jpg", JPEG, "image/jpeg")})
    assert peek.json()["kind"] == "image" and peek.json()["pictures_left_today"] == pictures.DAILY_PICTURES and calls == []   # a look reads nothing
    bad = await _add(client, h, brand, JPEG + b"1", name="bad.jpg")
    assert bad.status_code == 422 and "Nothing could be read" in bad.text
    for n in range(pictures.DAILY_PICTURES):
        assert (await _add(client, h, brand, JPEG + str(n).encode() * 3, name=f"p{n}.jpg")).status_code == 201, n
        await get_db()["sources"].update_many({"workspace_id": ws}, {"$set": {"status": "removed"}})                     # keep clear of the number-of-sources limit
    full = await _add(client, h, brand, JPEG + b"last", name="p-last.jpg")
    assert full.status_code == 429 and "pictures a day" in full.text


async def test_removing_a_file_source_clears_what_was_waiting(world):
    client, user, ws, brand, h = world
    made = (await _add(client, h, brand, _workbook(PRODUCTS))).json()
    assert await service.pending_col().count_documents({"source_id": made["id"]}) == 3
    assert (await client.delete(f"/api/v1/sources/{made['id']}", headers=h)).status_code == 200
    assert await service.pending_col().count_documents({"source_id": made["id"]}) == 0
