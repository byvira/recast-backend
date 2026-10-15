"""Bringing a video in from a Dropbox or Google Drive share link: which links are accepted and how they become direct addresses, and which are
refused with a plain reason. The network is not touched."""
import pytest

from app.video import link_import as L


def test_a_dropbox_share_link_becomes_a_direct_download_and_keeps_its_key():
    provider, url = L.direct_address("https://www.dropbox.com/scl/fi/abc123/clip.mp4?rlkey=k1&dl=0")
    assert provider == "dropbox"
    assert url.startswith("https://www.dropbox.com/scl/fi/abc123/clip.mp4?") and "rlkey=k1" in url and url.endswith("dl=1")


def test_a_drive_file_link_in_either_shape_becomes_the_download_address():
    for link in ("https://drive.google.com/file/d/1AbCdEfGhIjKlM/view?usp=sharing", "https://drive.google.com/open?id=1AbCdEfGhIjKlM",
                 "https://drive.google.com/uc?id=1AbCdEfGhIjKlM"):
        assert L.direct_address(link) == ("drive", "https://drive.google.com/uc?export=download&id=1AbCdEfGhIjKlM")


@pytest.mark.parametrize("link", [
    "http://www.dropbox.com/s/x/a.mp4", "https://evil.example/a.mp4", "https://www.dropbox.com.evil.example/s/x/a.mp4",
    "https://drive.google.com/drive/folders/1AbCdEfGhIjKlM", "https://www.dropbox.com/scl/fo/abc/xyz?dl=0",
    "https://drive.google.com/file/d/short/view", "", "x" * 1100,
])
def test_other_hosts_folders_and_odd_links_are_refused_with_a_reason(link):
    with pytest.raises(L.LinkError) as err:
        L.direct_address(link)
    assert str(err.value)


def test_a_download_may_only_end_on_the_providers_own_hosts():
    assert L.fetch_host_ok("https://uc1234.dl.dropboxusercontent.com/cd/0/get/x")
    assert L.fetch_host_ok("https://doc-0g-ak-docs.googleusercontent.com/docs/x")
    assert not L.fetch_host_ok("https://evil.example/x") and not L.fetch_host_ok("http://dl.dropboxusercontent.com/x")
    assert not L.fetch_host_ok("https://dropboxusercontent.com.evil.example/x")


def test_the_file_name_is_kept_to_plain_characters():
    assert L.name_from_headers('attachment; filename="My clip (1).mp4"', "f.mp4") == "My clip _1_.mp4"
    assert L.name_from_headers(None, "f.mp4") == "f.mp4"
