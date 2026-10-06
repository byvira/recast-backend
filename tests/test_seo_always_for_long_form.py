"""A Blog post always gets a title, summary and tags and a Newsletter a subject and preview line (the editors read them), whether or
not the optional SEO extra is on. YouTube keeps the option, and short platforms never get it."""
import pytest

from app.models.text import Platform
from app.pipelines.text.seo import should_run_seo


@pytest.mark.parametrize("platform", [Platform.BLOG, Platform.NEWSLETTER])
@pytest.mark.parametrize("option", [True, False])
def test_blog_and_newsletter_always_get_their_details(platform, option):
    assert should_run_seo(platform, option) is True


def test_youtube_follows_the_option():
    assert should_run_seo(Platform.YOUTUBE, True) is True
    assert should_run_seo(Platform.YOUTUBE, False) is False


@pytest.mark.parametrize("platform", [Platform.LINKEDIN, Platform.TWITTER])
def test_short_platforms_never_get_them(platform):
    assert should_run_seo(platform, True) is False
    assert should_run_seo(platform, False) is False
