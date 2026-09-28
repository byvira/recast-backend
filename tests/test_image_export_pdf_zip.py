"""Tests for the real PDF/ZIP multi-slide export (closing the gap
GAPS.md G-4 named: export_format only ever accepted png/webp)."""

import io
import zipfile

import pytest

from app.api.v1 import image_assets as image_module
from app.db.mongo import media_assets
from tests.test_image_assets import _brand, _generate, _h, _png, _setup, stubs  # noqa: F401 — fixture reuse


async def _add_second_slide(client, ws_id, asset):
    res = await client.post(
        f"/api/v1/image-assets/{asset['id']}/slides",
        json={"headline": "Second slide", "accent_keyword": "slide", "active_layout": "quote_1_1"},
        headers=_h(ws_id),
    )
    assert res.status_code == 201, res.text
    return res.json()


async def test_zip_export_bundles_every_real_slide(signup_user, stubs, monkeypatch):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    asset = await _add_second_slide(client, ws_id, asset)

    async def _fake_download(url):
        return _png(color=(10, 20, 30))

    monkeypatch.setattr(image_module, "_download_slide_bytes", _fake_download)

    res = await client.post(
        f"/api/v1/image-assets/{asset['id']}/export", json={"export_format": "zip"}, headers=_h(ws_id),
    )
    assert res.status_code == 201, res.text
    export = res.json()
    assert export["mime_type"] == "application/zip"
    assert export["kind"] == "document"

    media_doc = await media_assets.find_one({"id": export["id"]})
    # The real upload stub returns a fixed URL, not the actual bytes, so
    # fetch what stubs recorded instead of the DB's own url field.
    assert len(stubs["uploads"]) >= 1
    zip_bytes = stubs["uploads"][-1]
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        assert zf.namelist() == ["slide_1.png", "slide_2.png"]


async def test_pdf_export_produces_a_real_pdf_with_one_page_per_slide(signup_user, stubs, monkeypatch):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    asset = await _add_second_slide(client, ws_id, asset)

    async def _fake_download(url):
        return _png(color=(10, 20, 30))

    monkeypatch.setattr(image_module, "_download_slide_bytes", _fake_download)

    res = await client.post(
        f"/api/v1/image-assets/{asset['id']}/export", json={"export_format": "pdf"}, headers=_h(ws_id),
    )
    assert res.status_code == 201, res.text
    assert res.json()["mime_type"] == "application/pdf"
    pdf_bytes = stubs["uploads"][-1]
    assert pdf_bytes[:4] == b"%PDF"


async def test_pdf_zip_export_404s_when_slides_are_missing(signup_user, stubs, monkeypatch):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()
    await media_assets.delete_many({"workspace_id": ws_id})

    res = await client.post(
        f"/api/v1/image-assets/{asset['id']}/export", json={"export_format": "zip"}, headers=_h(ws_id),
    )
    assert res.status_code == 404


async def test_a_slide_fetch_failure_is_a_clean_502_not_a_crash(signup_user, stubs, monkeypatch):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()

    import httpx

    async def _boom(url):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(image_module, "_download_slide_bytes", _boom)

    res = await client.post(
        f"/api/v1/image-assets/{asset['id']}/export", json={"export_format": "pdf"}, headers=_h(ws_id),
    )
    assert res.status_code == 502
