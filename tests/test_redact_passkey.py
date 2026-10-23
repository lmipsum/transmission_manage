"""redact() and the logging filter. Synthetic passkey-shaped inputs live only in this file, which the
tracker-secret scanner allowlists (as upstream does for its test_redact_passkey.py)."""

import logging

import pytest

from modules.util import RedactingFilter
from modules.util import redact

HASH = "c" * 40


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://tracker.example.org/announce?passkey=abc123", "https://tracker.example.org/announce?passkey=***"),
        ("https://tracker.example.org/announce/0123456789abcdef0123", "https://tracker.example.org/announce/***"),
        ("https://t.example.org/0123456789abcdef0123/announce", "https://t.example.org/***/announce"),
        ("udp://t.example.org:1337/announce?uid=9&key=zz", "udp://t.example.org:1337/announce?uid=***&key=***"),
    ],
)
def test_redact_masks_secrets_in_urls(raw, expected):
    assert redact(f"tracker {raw} failed") == f"tracker {expected} failed"


def test_redact_keeps_bare_hashes_readable():
    assert redact(f"torrent {HASH} removed") == f"torrent {HASH} removed"


def test_redacting_filter_applies_to_formatted_records():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "url=%s", ("https://e.org/announce?passkey=s3cret",), None)
    RedactingFilter().filter(record)
    assert record.getMessage() == "url=https://e.org/announce?passkey=***"
