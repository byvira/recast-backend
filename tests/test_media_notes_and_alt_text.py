"""A post says when a planned or extra picture did not go out, and a picture's own description travels with it."""
from datetime import datetime, timezone

from app.models.media import MediaAsset, MediaKind, MediaSource
from app.pipelines.publish.attachments import media_snapshot
from app.pipelines.publish.spine import extra_media_note, planned_media_note


def _media_doc(**extra) -> dict:
    return MediaAsset(
        id="m1", workspace_id="w1", kind=MediaKind.IMAGE, url="https://res.cloudinary.com/demo/image/upload/v1/a.png",
        mime_type="image/png", source=MediaSource.RENDERED, created_by="u1", created_at=datetime.now(timezone.utc), **extra,
    ).model_dump()


def test_a_post_whose_planned_picture_could_not_be_made_says_so_when_it_goes_out():
    piece = {"media_status": {"image": "failed"}, "media": []}
    assert "could not be made" in planned_media_note(piece)
    assert "went out without one" in planned_media_note(piece)


def test_no_note_when_the_picture_was_made_or_attached_or_never_planned():
    assert planned_media_note({"media_status": {"image": "failed"}, "media": [{"id": "m1"}]}) is None
    assert planned_media_note({"media_status": {"image": "ready"}, "media": []}) is None
    assert planned_media_note({"media_status": None, "media": []}) is None
    assert planned_media_note({}) is None


def test_extra_attached_pictures_are_counted_in_the_note():
    two = {"attachments": [{"media_id": "a"}, {"media_id": "b"}]}
    assert "Only the first of 2 attached pictures was posted" in extra_media_note(two, "LinkedIn")
    assert extra_media_note({"attachments": [{"media_id": "a"}]}, "LinkedIn") is None
    five = {"attachments": [{"media_id": str(i)} for i in range(5)]}
    assert "Only the first 4 of 5" in extra_media_note(five, "Bluesky")
    assert extra_media_note({"attachments": [{"media_id": "a"}, {"media_id": "b"}]}, "Bluesky") is None


def test_the_pictures_description_is_kept_on_the_attached_copy():
    snap = media_snapshot(_media_doc(), alt_text="A calm sunrise over green hills")
    assert snap["alt_text"] == "A calm sunrise over green hills"


def test_a_picture_with_no_description_gets_no_alt_text_key_added():
    assert not media_snapshot(_media_doc()).get("alt_text")


def test_a_flagged_picture_is_marked_on_the_copy():
    assert media_snapshot(_media_doc(), qa_flagged=True)["qa_flagged"] is True
