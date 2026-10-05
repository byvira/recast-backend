"""One Activity Log row with an empty metadata field must not make the whole page fail to load."""
from datetime import datetime, timezone

from app.api.v1.activity import _scalar_metadata, present


def test_metadata_keeps_text_and_numbers_and_drops_empty_or_nested_values():
    metadata = {"event": "connection.reconnect_requested", "count": 3, "ratio": 0.5, "before": None, "after": None,
                "nested": {"a": 1}, "items": [1, 2], "flag": True, "off": False}
    assert _scalar_metadata(metadata) == {
        "event": "connection.reconnect_requested", "count": 3, "ratio": 0.5, "flag": "true", "off": "false",
    }
    assert _scalar_metadata({"before": None}) is None
    assert _scalar_metadata(None) is None
    assert _scalar_metadata("not a dict") is None


def test_a_row_with_empty_metadata_values_presents_with_clean_metadata():
    row = present({
        "_id": "system:platformops:1", "occurred_at": datetime.now(timezone.utc), "lane": "passive",
        "actor": {"name": "Recast", "type": "system_cron"}, "category": "platform_ops", "title": "YouTube: reconnect email sent",
        "description": "Sent.", "metadata": {"event": "connection.reconnect_requested", "before": None, "after": None, "reason": ""},
    })
    assert row["metadata"] == {"event": "connection.reconnect_requested", "reason": ""}
