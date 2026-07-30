"""Tests for the pure parsing helpers in the Gmail source."""

import email.utils
from email.parser import BytesParser

from activity.sources.gmail import body_preview
from activity.sources.gmail import decode_field
from activity.sources.gmail import header_timestamp
from activity.sources.gmail import is_bulk
from activity.sources.gmail import message_link
from activity.sources.gmail import parse_fetch
from activity.sources.gmail import recipients
from activity.sources.gmail import sender_name
from activity.sources.gmail import strip_html

HEADERS = (
    b"From: Jane Doe <jane@example.com>\r\n"
    b"To: you@gmail.com, other@example.com\r\n"
    b"Subject: Lunch?\r\n"
    b"Date: Tue, 28 Jul 2026 09:15:00 -0700\r\n"
    b"Message-ID: <abc123@example.com>\r\n\r\n"
)


def test_parse_fetch_extracts_uid_flags_and_body():
    response = [
        (b"1 (UID 42 FLAGS (\\Seen \\Flagged) BODY[HEADER.FIELDS (FROM)] {30}", HEADERS),
        b")",
        (b"2 (UID 43 FLAGS () BODY[HEADER.FIELDS (FROM)] {30}", HEADERS),
        b")",
    ]
    parsed = parse_fetch(response)
    assert [(uid, flags) for uid, flags, _ in parsed] == [
        (42, {"\\Seen", "\\Flagged"}),
        (43, set()),
    ]
    assert parsed[0][2] == HEADERS


def test_parse_fetch_ignores_untagged_and_bare_lines():
    assert parse_fetch([b"OK", (b"1 (FLAGS (\\Seen)", b"body")]) == []


def test_decode_field_handles_rfc2047():
    assert decode_field("=?utf-8?B?SGVsbG8gd29ybGQ=?=") == "Hello world"
    assert decode_field(None) == ""


def test_decode_field_unfolds_wrapped_headers():
    assert decode_field("Re: a long\r\n subject line") == "Re: a long subject line"


def test_sender_name_prefers_display_name_then_local_part():
    assert sender_name("Jane Doe <jane@example.com>") == "Jane Doe"
    assert sender_name("jane@example.com") == "jane"
    assert sender_name(None) == "unknown"


def test_header_timestamp_parses_date_header():
    ts = header_timestamp("Tue, 28 Jul 2026 09:15:00 -0700")
    assert email.utils.formatdate(ts, usegmt=True).startswith("Tue, 28 Jul 2026 16:15:00")


def test_header_timestamp_falls_back_for_broken_date():
    assert header_timestamp("not a date") > 0


def test_message_link_encodes_the_search_query():
    link = message_link("<abc+123@example.com>")
    assert link.endswith("#search/rfc822msgid%3Aabc%2B123%40example.com")


def test_is_bulk_detects_list_headers():
    assert is_bulk({"List-Id": "<dev.example.com>"})
    assert is_bulk({"List-Unsubscribe": "<mailto:x@y>"})
    assert not is_bulk({"From": "jane@example.com"})


def test_recipients_merges_to_and_cc_lowercased():
    headers = {"To": "You@Gmail.com", "Cc": "Other@Example.com"}
    assert recipients(headers) == ["you@gmail.com", "other@example.com"]


def parse(raw: bytes):
    return BytesParser().parsebytes(raw)


def test_body_preview_plain_text():
    msg = parse(b"Content-Type: text/plain\r\n\r\nLine one.\r\nLine two.\r\n")
    assert body_preview(msg) == "Line one. Line two."


def test_body_preview_prefers_plain_in_multipart():
    msg = parse(
        b"Content-Type: multipart/alternative; boundary=B\r\n\r\n"
        b"--B\r\nContent-Type: text/plain\r\n\r\nplain body\r\n"
        b"--B\r\nContent-Type: text/html\r\n\r\n<p>html body</p>\r\n"
        b"--B--\r\n"
    )
    assert body_preview(msg) == "plain body"


def test_body_preview_falls_back_to_stripped_html():
    msg = parse(
        b"Content-Type: text/html\r\n\r\n"
        b"<html><head><style>p{color:red}</style></head>"
        b"<body><p>Hello &amp; welcome</p></body></html>"
    )
    assert body_preview(msg) == "Hello & welcome"


def test_body_preview_skips_attachments_and_caps_length():
    msg = parse(
        b"Content-Type: multipart/mixed; boundary=B\r\n\r\n"
        b"--B\r\nContent-Type: text/plain\r\n\r\n" + b"word " * 100 + b"\r\n"
        b"--B\r\nContent-Type: text/plain\r\nContent-Disposition: attachment; "
        b'filename="notes.txt"\r\n\r\nATTACHMENT TEXT\r\n'
        b"--B--\r\n"
    )
    preview = body_preview(msg)
    assert "ATTACHMENT" not in preview
    assert len(preview) <= 200
    assert preview.endswith("…")


def test_body_preview_decodes_quoted_printable_and_charset():
    msg = parse(
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
        b"Caf=C3=A9 tomorrow?\r\n"
    )
    assert body_preview(msg) == "Café tomorrow?"


def test_body_preview_empty_message():
    msg = parse(b"Content-Type: text/plain\r\n\r\n")
    assert body_preview(msg) == ""


def test_strip_html_drops_style_and_script_blocks():
    text = strip_html("<style>a{}</style><script>x()</script><b>keep</b> this")
    assert text.split() == ["keep", "this"]
