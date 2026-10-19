"""A failure to write the Activity Log entry for a content safety change is logged, and the change itself still saves."""

import logging
from unittest.mock import AsyncMock, patch

from tests.test_content_guard import _staff


async def test_failed_audit_entry_is_logged_and_the_settings_still_save(make_client, caplog):
    owner, _ = await _staff(make_client, master=True)
    broken = AsyncMock(side_effect=RuntimeError("activity store is down"))
    with patch("app.api.v1.content_guard.record_system", new=broken), caplog.at_level(logging.WARNING):
        res = await owner.put("/api/v1/ops/content-safety", json={"version": 0, "strictness": "strict"})

    assert res.status_code == 200 and res.json()["version"] == 1
    broken.assert_awaited()
    assert any("Content safety settings" in rec.getMessage() for rec in caplog.records)
