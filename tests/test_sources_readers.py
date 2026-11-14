"""Stage 1 of sources: the safe fetch, the feed readers and the file readers. No network and no database."""
import asyncio
import io
import zipfile

import httpx
import pytest

from app.pipelines.publish.generic.safe_url import UnsafeUrl, check_https_url, check_resolved_public
from app.sources import documents, feeds, fetch as fetcher
from app.sources.items import ReadError, canonical_link, item_id, plain


def run(coro):
    return asyncio.run(coro)


# ── the safe fetch ───────────────────────────────────────────────────────────

def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


async def _allow(url):
    check_https_url(url)          # the shape checks stay on; only the DNS lookup is replaced


def test_a_normal_answer_is_returned_with_its_type_and_final_address():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, content=b"<rss></rss>", headers={"content-type": "application/rss+xml; charset=utf-8"})

    got = run(fetcher.fetch("https://example.com/feed", client=_client(handler), guard=_allow))
    assert got.content_type == "application/rss+xml" and got.body == b"<rss></rss>" and got.url == "https://example.com/feed" and seen == ["https://example.com/feed"]


@pytest.mark.parametrize("url", ["http://example.com/feed", "https://user:pass@example.com/feed", "https://localhost/feed", "https://metadata.google.internal/x", "ftp://example.com/x", "https://intranet.local/x"])
def test_an_unsafe_address_is_refused_before_any_request_is_made(url):
    called = []

    def handler(request):
        called.append(1)
        return httpx.Response(200)

    with pytest.raises(fetcher.FetchError) as caught:
        run(fetcher.fetch(url, client=_client(handler), guard=_allow))
    assert caught.value.code == "address_refused" and called == []


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "192.168.1.9", "::1", "172.16.0.1"])
def test_private_loopback_and_metadata_addresses_are_never_public(address):
    with pytest.raises(UnsafeUrl):
        check_resolved_public(address)


def test_a_redirect_to_a_private_address_is_refused_at_the_hop_not_followed():
    async def guard(url):
        check_https_url(url)
        if "internal.example" in url:
            raise UnsafeUrl("That address points inside a private network.")

    def handler(request):
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "https://internal.example/secret"})
        raise AssertionError("the private address must never be requested")

    with pytest.raises(fetcher.FetchError) as caught:
        run(fetcher.fetch("https://example.com/feed", client=_client(handler), guard=guard))
    assert caught.value.code == "address_refused"


def test_a_redirect_to_plain_http_is_refused_and_too_many_redirects_stop():
    with pytest.raises(fetcher.FetchError) as caught:
        run(fetcher.fetch("https://example.com/a", client=_client(lambda r: httpx.Response(301, headers={"location": "http://example.com/b"})), guard=_allow))
    assert caught.value.code == "address_refused"
    with pytest.raises(fetcher.FetchError) as caught:
        run(fetcher.fetch("https://example.com/a", client=_client(lambda r: httpx.Response(302, headers={"location": "https://example.com/a"})), guard=_allow))
    assert caught.value.code == "too_many_redirects"


def test_a_relative_redirect_is_followed_once_and_checked():
    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        return httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})

    got = run(fetcher.fetch("https://example.com/old", client=_client(handler), guard=_allow))
    assert got.url == "https://example.com/new" and got.body == b"ok"


def test_status_codes_become_plain_refusals_and_only_busy_ones_may_work_later():
    cases = {401: ("needs_login", False), 403: ("needs_login", False), 404: ("not_found", False), 410: ("not_found", False), 429: ("site_busy", True), 503: ("site_busy", True), 400: ("cant_open", False)}
    for status, (code, retry) in cases.items():
        with pytest.raises(fetcher.FetchError) as caught:
            run(fetcher.fetch("https://example.com/x", client=_client(lambda r, s=status: httpx.Response(s)), guard=_allow))
        assert caught.value.code == code and caught.value.retryable is retry, status


def test_an_oversize_answer_is_refused_and_never_fully_read():
    with pytest.raises(fetcher.FetchError) as caught:
        run(fetcher.fetch("https://example.com/big", max_bytes=1000, client=_client(lambda r: httpx.Response(200, content=b"x" * 5000)), guard=_allow))
    assert caught.value.code == "too_large"


def test_a_timeout_and_a_connection_failure_may_work_at_the_next_check():
    def slow(request):
        raise httpx.ReadTimeout("slow")

    def down(request):
        raise httpx.ConnectError("down")

    for handler, code in ((slow, "too_slow"), (down, "cant_open")):
        with pytest.raises(fetcher.FetchError) as caught:
            run(fetcher.fetch("https://example.com/x", client=_client(handler), guard=_allow))
        assert caught.value.code == code and caught.value.retryable is True


# ── feeds ────────────────────────────────────────────────────────────────────

RSS = b"""<?xml version="1.0"?><rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><title>Daily Brew</title>
<item><title>First &amp; best</title><link>https://example.com/a?utm_source=x</link><guid>g1</guid><pubDate>Tue, 10 Jun 2025 09:00:00 GMT</pubDate>
<description>&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;</description></item>
<item><title>Second</title><link>https://example.com/b</link><content:encoded><![CDATA[<div>Full <i>body</i> here</div>]]></content:encoded></item>
<item><description>no title or link</description></item></channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Notes</title>
<entry><title>Atom one</title><link rel="alternate" href="https://example.com/atom1"/><id>tag:1</id><updated>2025-06-10T09:00:00Z</updated><summary>Short &lt;em&gt;summary&lt;/em&gt;</summary></entry>
<entry><title>Atom two</title><link rel="self" href="https://example.com/feed"/><link href="https://example.com/atom2"/><id>tag:2</id><content>Body two</content></entry></feed>"""

JSONFEED = b'{"version": "https://jsonfeed.org/version/1.1", "title": "JF", "items": [{"id": "1", "url": "https://example.com/j1", "title": "J one", "content_text": "Plain body", "date_published": "2025-06-10T09:00:00Z"}, {"id": "2", "content_html": "<p>No url</p>", "title": "J two"}, "junk"]}'


def test_rss_is_read_into_clean_items_and_an_item_with_nothing_to_show_is_skipped():
    title, items = feeds.parse(RSS)
    assert title == "Daily Brew" and [i.title for i in items] == ["First & best", "Second"]
    assert items[0].text == "Hello world" and items[0].published.year == 2025 and items[1].text == "Full body here"
    assert items[0].link.startswith("https://example.com/a")


def test_atom_is_read_and_the_alternate_link_is_the_one_used():
    title, items = feeds.parse(ATOM)
    assert title == "Notes" and [i.link for i in items] == ["https://example.com/atom1", "https://example.com/atom2"] and items[0].text == "Short summary"


def test_json_feed_is_read_and_junk_inside_it_is_ignored():
    title, items = feeds.parse(JSONFEED)
    assert title == "JF" and [i.title for i in items] == ["J one", "J two"] and items[0].published is not None


def test_the_same_article_with_a_tracking_tag_or_slash_is_one_item():
    assert canonical_link("https://Example.com/a/?utm_source=x&b=2&fbclid=9#top") == "https://example.com/a?b=2"
    assert item_id("https://example.com/a?utm_medium=y") == item_id("https://example.com/a/")
    _, items = feeds.parse(RSS)
    assert items[0].id == item_id("https://example.com/a")


def test_a_feed_with_a_doctype_or_entities_is_refused_before_it_is_parsed():
    bomb = b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]><rss><channel><title>&lol2;</title></channel></rss>'
    xxe = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss><channel><title>&x;</title></channel></rss>'
    for body in (bomb, xxe):
        with pytest.raises(ReadError) as caught:
            feeds.parse(body)
        assert caught.value.code == "unsafe_feed"


@pytest.mark.parametrize("body", [b"<html><body>Not a feed</body></html>", b"just words", b"{\"items\": []}", b"<rss", b""])
def test_anything_that_is_not_a_feed_says_so_in_plain_words(body):
    with pytest.raises(ReadError) as caught:
        feeds.parse(body)
    assert caught.value.code == "not_a_feed" and "feed" in caught.value.message


def test_a_huge_feed_is_cut_at_fifty_items_and_an_oversize_one_is_refused():
    many = b'<rss version="2.0"><channel><title>T</title>' + b"".join(b"<item><title>Item %d</title><link>https://example.com/%d</link></item>" % (n, n) for n in range(200)) + b"</channel></rss>"
    assert len(feeds.parse(many)[1]) == feeds.MAX_ITEMS
    with pytest.raises(ReadError) as caught:
        feeds.parse(b" " * (feeds.MAX_FEED_BYTES + 1))
    assert caught.value.code == "too_large"
    assert feeds.looks_like_feed(RSS) and feeds.looks_like_feed(ATOM) and feeds.looks_like_feed(JSONFEED) and not feeds.looks_like_feed(b"<html></html>")


def test_markup_in_a_feed_never_survives_as_markup():
    assert "<" not in plain('<script>alert(1)</script><b>bold</b> &lt;tag&gt;')[:20] or plain('<script>alert(1)</script>x') == "alert(1) x"
    _, items = feeds.parse(b'<rss version="2.0"><channel><title>T</title><item><title>&lt;img src=x onerror=1&gt;Hi</title><link>https://e.com/1</link></item></channel></rss>')
    assert "<" not in items[0].title and "onerror" in items[0].title.lower() or "<img" not in items[0].title


# ── files ────────────────────────────────────────────────────────────────────

def _docx(text: str) -> bytes:
    from docx import Document

    document = Document()
    document.add_paragraph(text)
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text, table.rows[0].cells[1].text = "Plan", "Free"
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def _pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref)


def test_the_kind_of_a_file_is_what_it_really_is_not_what_it_is_called():
    assert documents.sniff(_pdf("Hello"), "x.txt") == documents.PDF
    assert documents.sniff(_docx("Hello"), "x.pdf") == documents.DOCX
    assert documents.sniff(b"\x89PNG\r\n\x1a\n" + b"0" * 20, "notes.pdf") == documents.IMAGE
    assert documents.sniff(b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 50, "report.pdf") == documents.OTHER
    assert documents.sniff(b"a,b\n1,2\n", "data.csv") == documents.CSV and documents.sniff(b"plain words", "n.txt") == documents.TEXT
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", "<x/>")
    assert documents.sniff(buf.getvalue(), "t.docx") == documents.XLSX


def test_pdf_word_and_text_are_read_into_one_item_with_a_title_from_the_name():
    title, items = documents.read("Launch notes.pdf", _pdf("Quarterly launch plan for the new product line and its pricing"))
    assert title == "Launch notes" and len(items) == 1 and "launch plan" in items[0].text and items[0].kind == "pdf"
    title, items = documents.read("brief.docx", _docx("A long enough paragraph about the brand voice and tone for testing"))
    assert "brand voice" in items[0].text and "Plan | Free" in items[0].text and items[0].kind == "docx"
    title, items = documents.read("idea.md", b"# Idea\nSome words that are long enough to count as a real document.")
    assert items[0].kind == "text" and "Some words" in items[0].text


def test_a_disguised_empty_oversize_or_unsupported_file_is_refused_with_a_reason():
    for name, data, code in (("a.pdf", b"", "empty"), ("a.pdf", b"x" * (documents.MAX_BYTES + 1), "too_large"), ("report.pdf", b"MZ\x90\x00" + b"\x00" * 60, "unsupported"),
                             ("photo.pdf", b"\x89PNG\r\n\x1a\n" + b"0" * 30, "image_not_here"), ("movie.docx", b"\x00\x00\x00\x18ftypmp42" + b"0" * 30, "video_not_here")):
        with pytest.raises(ReadError) as caught:
            documents.read(name, data)
        assert caught.value.code == code, name
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", "<x/>")
    with pytest.raises(ReadError) as caught:
        documents.read("t.xlsx", buf.getvalue())
    assert caught.value.code == "cant_read"                                                          # a zip that only looks like a workbook is a plain refusal


def test_a_pdf_with_no_text_says_so_instead_of_returning_nothing():
    with pytest.raises(ReadError) as caught:
        documents.read("scan.pdf", _pdf(""))
    assert caught.value.code == "cant_read" and "scanned" in caught.value.message.lower()


def test_csv_becomes_one_item_per_row_with_a_detected_header_and_chosen_columns():
    data = b"Title,Body,Notes\nLaunch day,We ship on Friday,internal\nPricing,New plans are live,\n,,\n"
    header, items = documents.read_csv(data, title_column="Title", text_columns=["Body"])
    assert header == ["Title", "Body", "Notes"] and [i.title for i in items] == ["Launch day", "Pricing"] and items[0].text == "Body: We ship on Friday" and items[1].extra["row"] == 2
    assert len({i.id for i in items}) == 2
    _, defaults = documents.read_csv(data)
    assert "Notes: internal" in defaults[0].text                                                    # no choice made: every other column is text


def test_csv_handles_semicolons_tabs_other_encodings_and_no_header():
    assert documents.read_csv("Name;Said\nAna;Hola\n".encode("utf-8"))[1][0].text == "Said: Hola"
    assert documents.read_csv("Name\tSaid\nAna\tHola\n".encode("utf-16"))[1][0].title == "Ana"
    assert documents.read_csv("Café,Ñandú\nbueno,sí\n".encode("cp1252"))[1][0].title == "bueno"
    header, items = documents.read_csv(b"alpha,beta\ngamma,delta\n")
    assert len(items) >= 1 and header[0]


def test_csv_limits_hold_and_an_empty_file_says_so():
    with pytest.raises(ReadError) as caught:
        documents.read_csv(b"")
    assert caught.value.code == "empty"
    with pytest.raises(ReadError) as caught:
        documents.read_csv(("h," * 50 + "\n1\n").encode())
    assert caught.value.code == "too_wide"
    rows = b"title,body\n" + b"".join(b"t%d,b%d\n" % (n, n) for n in range(documents.MAX_ROWS + 5))
    with pytest.raises(ReadError) as caught:
        documents.read_csv(rows)
    assert caught.value.code == "too_many_rows"
    _, items = documents.read_csv(b"title,body\nx," + b"y" * 9000 + b"\n")
    assert len(items[0].text) < documents.MAX_CELL + 20


def test_a_spreadsheet_formula_is_text_and_never_run():
    _, items = documents.read_csv(b'title,body\nsum,"=HYPERLINK(""http://evil"",""x"")"\n')
    assert "=HYPERLINK" in items[0].text                      # kept as plain words for the writer to see, never evaluated


def test_windows_text_is_not_misread_as_utf_16_in_either_reader():
    from app.pipelines.campaigns import source_files

    raw = "Café Ñandú sí buenos".encode("cp1252")                   # an even number of bytes, which "decodes" as UTF-16 garbage
    assert len(raw) % 2 == 0 and source_files._text_file(raw) == "Café Ñandú sí buenos" and documents._decode(raw) == "Café Ñandú sí buenos"
    assert documents._decode("hello wörld".encode("utf-16")) == "hello wörld" and documents._decode("héllo".encode("utf-8-sig")) == "héllo"
