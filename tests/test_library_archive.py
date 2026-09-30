"""Library export planning and ZIP assembly (app.pipelines.export.library_archive). Pure."""

import io
import zipfile

from app.pipelines.export import library_archive as la

PIECES = [
    {"content": "Why systems beat willpower\nBody text", "platform": "LinkedIn", "pipeline_type": "text"},
    {"content": "Why systems beat willpower\nAnother", "platform": "LinkedIn", "pipeline_type": "text"},
    {
        "content": "Launch day", "platform": "Instagram", "pipeline_type": "text",
        "media": [{"url": "https://x/a.png", "mime_type": "image/png", "kind": "image"}],
    },
]
AUDIO = [{"title": "Episode 1", "source_type": "script_tts", "media_id": "m1"}, {"title": "No file", "media_id": "gone"}]
IMAGES = [{"title": "Quote card", "slides": [
    {"slide_number": 1, "media_id": "m2", "text_content": {"headline": "Hi"}},
    {"slide_number": 2, "media_id": "m3", "text_content": {}},
]}]
MEDIA = {
    "m1": {"url": "https://x/e.mp3", "mime_type": "audio/mpeg"},
    "m2": {"url": "https://x/c1.png", "mime_type": "image/png"},
    "m3": {"url": "https://x/c2.webp", "mime_type": "image/webp"},
}


def plan():
    return la.plan_library_export(PIECES, AUDIO, IMAGES, MEDIA)


def names(folder):
    return [f.name for f in plan() if f.folder == folder]


def test_text_is_one_txt_per_post_in_the_required_pattern():
    text = names("text")
    assert text[0] == "why-systems-beat-willpower_linkedin_text_post.txt"
    assert all(n.endswith(".txt") for n in text)


def test_repeated_names_are_made_unique():
    text = names("text")
    assert text[1] == "why-systems-beat-willpower_linkedin_text_post-2.txt"
    assert len(set(text)) == len(text)


def test_media_keeps_native_formats_and_the_pattern():
    media = names("media")
    assert "launch-day_instagram_text_image.png" in media
    assert "episode-1_library_audio_narration.mp3" in media
    assert "quote-card-1_library_image_card.png" in media
    assert "quote-card-2_library_image_image.webp" in media
    assert not any(n.endswith(".md") for n in media)


def test_an_audio_asset_with_no_stored_file_is_left_out():
    assert not any("no-file" in n for n in names("media"))


def test_media_limit_keeps_all_text():
    many = [{"title": f"t{i}", "slides": [{"slide_number": 1, "media_id": "m2"}]} for i in range(la.MAX_MEDIA_FILES + 5)]
    planned = la.plan_library_export(PIECES, [], many, MEDIA)
    kept, skipped = la.within_limits(planned)
    # 205 images plus the one attached to a post, minus the 200 kept.
    assert skipped == 6
    assert len([f for f in kept if f.folder == "text"]) == len(PIECES)
    assert len([f for f in kept if f.folder == "media"]) == la.MAX_MEDIA_FILES


def test_zip_has_text_and_media_folders_and_a_readme():
    files = plan()
    media_bytes = {f.name: b"DATA" for f in files if f.folder == "media"}
    zf = zipfile.ZipFile(io.BytesIO(la.build_zip(files, media_bytes, [])))
    listed = zf.namelist()
    assert "README.txt" in listed
    assert any(n.startswith("text/") for n in listed) and any(n.startswith("media/") for n in listed)
    assert zf.read("text/why-systems-beat-willpower_linkedin_text_post.txt").decode().startswith("Why systems")
    assert zf.read("media/episode-1_library_audio_narration.mp3") == b"DATA"


def test_a_file_that_could_not_be_downloaded_is_named_in_the_readme():
    files = plan()
    media_bytes = {f.name: b"x" for f in files if f.folder == "media" and "episode" not in f.name}
    zf = zipfile.ZipFile(io.BytesIO(la.build_zip(files, media_bytes, ["Episode 1"], skipped_for_limit=3)))
    assert not any("episode-1" in n for n in zf.namelist())
    readme = zf.read("README.txt").decode()
    assert "Episode 1" in readme and "3 more media files" in readme


def test_an_empty_library_still_makes_a_valid_zip():
    zf = zipfile.ZipFile(io.BytesIO(la.build_zip([], {}, [])))
    assert zf.namelist() == ["README.txt"]
