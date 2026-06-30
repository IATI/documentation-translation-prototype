"""Tests for PO file utilities.

strip_obsolete is the regression for "bugfix: remove all obsolete translations".
"""

import polib

from translation_lib.po_utils import (
    clear_needs_review,
    get_needs_review,
    get_review_fingerprint,
    is_locked,
    set_needs_review,
    set_review_fingerprint,
    strip_obsolete,
)


def _make_obsolete(msgid, msgstr):
    entry = polib.POEntry(msgid=msgid, msgstr=msgstr)
    entry.obsolete = 1
    return entry


def test_strip_obsolete_removes_obsolete_entries():
    po = polib.POFile()
    po.metadata = {"Content-Type": "text/plain; charset=utf-8"}
    po.append(polib.POEntry(msgid="live", msgstr="vivant"))
    po.append(_make_obsolete("old one", "ancien"))
    po.append(_make_obsolete("old two", "ancien deux"))

    removed = strip_obsolete(po)

    assert removed == 2
    assert po.obsolete_entries() == []
    assert [e.msgid for e in po] == ["live"]


def test_strip_obsolete_noop_when_none():
    po = polib.POFile()
    po.metadata = {"Content-Type": "text/plain; charset=utf-8"}
    po.append(polib.POEntry(msgid="live", msgstr="vivant"))

    assert strip_obsolete(po) == 0
    assert [e.msgid for e in po] == ["live"]


def test_is_locked_detects_marker():
    entry = polib.POEntry(msgid="x", msgstr="y", tcomment="LOCKED")
    assert is_locked(entry) is True


def test_is_locked_with_reason():
    entry = polib.POEntry(msgid="x", msgstr="y", tcomment="LOCKED: adjusted per user feedback")
    assert is_locked(entry) is True


def test_is_locked_ignores_other_comments():
    entry = polib.POEntry(msgid="x", msgstr="y", tcomment="translator note")
    assert is_locked(entry) is False


def test_is_locked_no_comment():
    entry = polib.POEntry(msgid="x", msgstr="y")
    assert is_locked(entry) is False


# --- review fingerprint markers ---------------------------------------------


def test_review_fingerprint_round_trip():
    entry = polib.POEntry(msgid="x", msgstr="y")
    assert get_review_fingerprint(entry) is None
    assert set_review_fingerprint(entry, "abc123") is True
    assert get_review_fingerprint(entry) == "abc123"


def test_set_review_fingerprint_noop_when_unchanged():
    entry = polib.POEntry(msgid="x", msgstr="y")
    set_review_fingerprint(entry, "abc123")
    # Second identical write should report "no change" so callers skip saving.
    assert set_review_fingerprint(entry, "abc123") is False
    assert set_review_fingerprint(entry, "def456") is True
    assert get_review_fingerprint(entry) == "def456"


def test_review_fingerprint_coexists_with_locked():
    entry = polib.POEntry(msgid="x", msgstr="y", tcomment="LOCKED: keep")
    set_review_fingerprint(entry, "abc123")
    # Both markers survive: LOCKED is still detected and the fingerprint reads back.
    assert is_locked(entry) is True
    assert get_review_fingerprint(entry) == "abc123"


# --- needs-review markers ----------------------------------------------------


def test_needs_review_round_trip():
    entry = polib.POEntry(msgid="x", msgstr="y")
    assert get_needs_review(entry) is None
    set_needs_review(entry, "fp123", "length ratio too low")
    assert get_needs_review(entry) == ("fp123", "length ratio too low")


def test_needs_review_and_reviewed_are_mutually_exclusive():
    entry = polib.POEntry(msgid="x", msgstr="y")
    set_needs_review(entry, "fp123", "stale anchor")
    # Passing review later must drop the needs-review marker.
    set_review_fingerprint(entry, "abc123")
    assert get_needs_review(entry) is None
    assert get_review_fingerprint(entry) == "abc123"
    # And going back to needs-review must drop the reviewed fingerprint.
    set_needs_review(entry, "fp456", "regressed")
    assert get_review_fingerprint(entry) is None
    assert get_needs_review(entry) == ("fp456", "regressed")


def test_needs_review_coexists_with_locked():
    entry = polib.POEntry(msgid="x", msgstr="y", tcomment="LOCKED: keep")
    set_needs_review(entry, "fp123", "reason")
    assert is_locked(entry) is True
    assert get_needs_review(entry) == ("fp123", "reason")


def test_clear_needs_review():
    entry = polib.POEntry(msgid="x", msgstr="y")
    assert clear_needs_review(entry) is False
    set_needs_review(entry, "fp123", "reason")
    assert clear_needs_review(entry) is True
    assert get_needs_review(entry) is None
