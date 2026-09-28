"""Tests for the real WCAG contrast-ratio check (app.pipelines.media.
contrast_check) and its endpoint on image-assets."""

import pytest

from app.pipelines.media.contrast_check import check_slide_contrast, contrast_ratio
from tests.test_image_assets import _generate, _h, _setup, stubs  # noqa: F401 — fixture reuse


def test_white_on_black_is_the_real_maximum_ratio():
    assert contrast_ratio("#FFFFFF", "#000000") == 21.0


def test_ratio_is_symmetric_regardless_of_argument_order():
    assert contrast_ratio("#FFFFFF", "#123456") == contrast_ratio("#123456", "#FFFFFF")


def test_identical_colors_have_the_real_minimum_ratio_of_one():
    assert contrast_ratio("#6366f1", "#6366f1") == 1.0


def test_check_slide_contrast_flags_a_real_failing_pair():
    results = check_slide_contrast(headline_fg_hex="#FFFFFF", accent_hex="#38bdf8", background_hex="#e5e5e5")
    headline = next(r for r in results if r.pair == "headline")
    assert headline.passes_aa_normal_text is False  # white on near-white genuinely fails
    assert headline.ratio < 4.5


def test_check_slide_contrast_passes_a_real_strong_pair():
    results = check_slide_contrast(headline_fg_hex="#FFFFFF", accent_hex="#38bdf8", background_hex="#0f172a")
    headline = next(r for r in results if r.pair == "headline")
    assert headline.passes_aaa_normal_text is True


async def test_contrast_endpoint_returns_the_real_brand_colors(signup_user, stubs):
    client, _, ws_id, brand_id = await _setup(signup_user)
    asset = (await _generate(client, ws_id, brand_id)).json()

    res = await client.get(f"/api/v1/image-assets/{asset['id']}/contrast-check", headers=_h(ws_id))
    assert res.status_code == 200, res.text
    results = res.json()
    assert {r["pair"] for r in results} == {"headline", "accent_keyword"}
    assert all("ratio" in r and "passes_aa_normal_text" in r for r in results)


async def test_contrast_endpoint_404s_for_a_missing_asset(signup_user, stubs):
    client, _, ws_id, _ = await _setup(signup_user)
    res = await client.get("/api/v1/image-assets/nope/contrast-check", headers=_h(ws_id))
    assert res.status_code == 404
