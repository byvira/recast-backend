"""Faults found by the text-writing sweep: each test failed before its fix."""
import pytest

from app.pipelines.text.language_check import language_problem

TAMIL = "எங்கள் புதிய சேவை இன்று தொடங்குகிறது. உங்கள் வேலையை எளிதாக்க நாங்கள் உதவுவோம். இன்றே சேருங்கள், இலவசமாக முயற்சி செய்யுங்கள்."
HINDI = "हमारी नई सेवा आज शुरू हो रही है। आपका काम आसान बनाने में हम मदद करेंगे। आज ही जुड़ें और मुफ्त में आज़माएं।"
TAGS = " #Ekavo #ContentCreation #SocialMediaMarketing #ProductLaunch #DigitalMarketing https://ekavo.example.com/launch-announcement-2026 @ekavo_team hello@ekavo.example.com"


@pytest.mark.parametrize("text, code", [(TAMIL, "ta"), (HINDI, "hi"), (TAMIL, "ta-IN"), (HINDI, "mr")])
def test_hashtags_links_and_mentions_do_not_make_a_native_post_look_foreign(text, code):
    assert language_problem(text, code) is None
    assert language_problem(text + TAGS, code) is None, "a good post was held only because of its hashtags and link"


def test_an_english_post_dressed_in_native_hashtags_is_still_caught():
    english = "We are launching our new service today and we would love for you to try it for free and tell us what you think about it."
    assert language_problem(english + " #தமிழ் #சேவை #இன்று", "ta") is not None
    assert language_problem(english + " #हिंदी #सेवा", "hi") is not None


def test_a_mixed_post_is_judged_on_its_words_not_on_its_link_and_hashtags():
    tanglish = "Namma puthu service innaiku start aagudhu, romba nalla irukku, neenga try pannunga, ungaluku nalla udhavum, vanga paakalam"
    assert language_problem(tanglish, "ta+en") is None
    assert language_problem(tanglish + TAGS, "ta+en") is None
    english = "Our new service starts today and it is very good, you should try it and see how much it helps your work every single day"
    assert language_problem(english + TAGS, "ta+en") is not None, "padding with hashtags must not turn English into a mix"


def test_native_letters_in_a_mixed_post_are_still_refused_even_inside_a_hashtag():
    tanglish = "Namma puthu service innaiku start aagudhu, romba nalla irukku, neenga try pannunga, ungaluku nalla udhavum, vanga paakalam"
    assert language_problem(tanglish + " #தமிழ்", "ta+en") is not None


def test_a_post_that_is_only_hashtags_and_a_link_is_not_judged():
    assert language_problem("#Ekavo #Launch #Today https://ekavo.example.com/launch-announcement-2026 @team", "ta") is None


# ---- length in languages written without spaces between words ----------------------------------------------------------------
import asyncio

from app.models.text import Platform
from app.pipelines.text.generator import validate_content
from app.pipelines.text.language_check import measured_words
from app.pipelines.text.quality import run_quality_gate

JAPANESE = "私たちの新しいサービスが今日から始まります。あなたの仕事をもっと簡単にするために、私たちはここにいます。今すぐ参加して、無料でお試しください。" * 6
CHINESE = "我们的新服务今天正式推出。为了让您的工作更轻松，我们随时为您服务。现在就加入，免费试用，了解更多。" * 8
THAI = "บริการใหม่ของเราเริ่มวันนี้ เรายินดีช่วยให้งานของคุณง่ายขึ้น เข้าร่วมวันนี้และลองใช้ฟรี" * 8
KOREAN = "우리의 새로운 서비스가 오늘 시작됩니다. 당신의 일을 더 쉽게 만들기 위해 우리가 도와드리겠습니다. 오늘 참여하고 무료로 사용해 보세요. " * 8


def test_words_are_counted_the_way_each_script_is_written():
    assert measured_words("one two three") == 3 and measured_words("") == 0
    assert measured_words(TAMIL) == len(TAMIL.split()), "scripts that use spaces are counted by their spaces, as before"
    assert measured_words(JAPANESE) > 150 and measured_words(CHINESE) > 150 and measured_words(THAI) > 150
    assert measured_words("今日は晴れです") < 10
    assert measured_words("Launch day 今日は晴れです ok") == 3 + 4, "spaced words plus the unspaced part, counted together"


@pytest.mark.parametrize("text, code", [(JAPANESE, "ja"), (CHINESE, "zh"), (THAI, "th")])
def test_a_full_length_post_in_a_language_without_spaces_is_not_called_too_short(text, code):
    gate = asyncio.run(run_quality_gate(text, Platform.LINKEDIN, "", [], language=code))
    assert not any("too short" in i.lower() for i in gate.issues), gate.issues
    ok, issues = validate_content(text, Platform.LINKEDIN, [], [], [], [], language=code)
    assert not any("too short" in i.lower() for i in issues), issues


@pytest.mark.parametrize("text, code", [("今日は晴れです。", "ja"), ("今天天气很好。", "zh"), ("วันนี้อากาศดี", "th")])
def test_a_really_short_post_in_those_languages_is_still_too_short(text, code):
    gate = asyncio.run(run_quality_gate(text, Platform.LINKEDIN, "", [], language=code))
    assert any("too short" in i.lower() for i in gate.issues)


def test_english_and_spaced_languages_are_counted_exactly_as_before():
    short = "A short English post."
    gate = asyncio.run(run_quality_gate(short, Platform.LINKEDIN, "", []))
    assert any("too short" in i.lower() and "4 words" in i for i in gate.issues)
    assert measured_words(KOREAN) == len(KOREAN.split())


def test_the_video_opening_line_minimum_counts_speech_the_way_the_language_is_written():
    from app.pipelines.text.language_check import measured_words

    speech = "今日は新しいサービスについてお話しします。みなさんの仕事をもっと楽にするための機能です。"
    assert len(speech.split()) < 8 <= measured_words(speech), "a spoken Japanese clip is long enough to write from"
