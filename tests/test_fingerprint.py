"""Tests for review fingerprints (the anti-churn mechanism).

A fingerprint marks a translation as "already reviewed under this standard".
Re-runs skip such entries; changing the standard (e.g. the glossary) must
invalidate them so the translations get refreshed.
"""

import polib

from translation_lib.config import TranslationConfig
from translation_lib.fingerprint import (
    entry_fingerprint,
    is_review_current,
    needs_review_current,
    standard_version,
)
from translation_lib.po_utils import set_needs_review, set_review_fingerprint


def _config(glossary=None, notes=""):
    return TranslationConfig(glossary=glossary or {}, notes=notes)


def test_entry_fingerprint_changes_with_translation():
    entry = polib.POEntry(msgid="Activity", msgstr="Activité")
    fp_before = entry_fingerprint(entry, "fr", "STD")
    entry.msgstr = "Activite"
    assert entry_fingerprint(entry, "fr", "STD") != fp_before


def test_entry_fingerprint_changes_with_standard():
    entry = polib.POEntry(msgid="Activity", msgstr="Activité")
    assert entry_fingerprint(entry, "fr", "STD1") != entry_fingerprint(entry, "fr", "STD2")


def test_standard_version_changes_with_glossary():
    """The whole point: editing the glossary shifts the standard version."""
    base = _config(glossary={"activity": {"fr": "activité"}})
    changed = _config(glossary={"activity": {"fr": "action"}})
    assert standard_version(base, "fr") != standard_version(changed, "fr")


def test_standard_version_changes_with_notes():
    assert standard_version(_config(notes="be formal"), "fr") != standard_version(
        _config(notes="be very formal"), "fr"
    )


def test_standard_version_stable_for_same_config():
    cfg = _config(glossary={"activity": {"fr": "activité"}}, notes="x")
    assert standard_version(cfg, "fr") == standard_version(cfg, "fr")


def test_is_review_current_true_when_stamped_and_unchanged():
    cfg = _config(glossary={"activity": {"fr": "activité"}})
    std = standard_version(cfg, "fr")
    entry = polib.POEntry(msgid="Activity", msgstr="Activité")
    set_review_fingerprint(entry, entry_fingerprint(entry, "fr", std))
    assert is_review_current(entry, "fr", std) is True


def test_is_review_current_false_when_unstamped():
    cfg = _config()
    entry = polib.POEntry(msgid="Activity", msgstr="Activité")
    assert is_review_current(entry, "fr", standard_version(cfg, "fr")) is False


def test_is_review_current_false_after_glossary_change():
    """A stamped entry goes stale when the glossary evolves, forcing re-review."""
    before = _config(glossary={"activity": {"fr": "activité"}})
    entry = polib.POEntry(msgid="Activity", msgstr="Activité")
    set_review_fingerprint(
        entry, entry_fingerprint(entry, "fr", standard_version(before, "fr"))
    )

    after = _config(glossary={"activity": {"fr": "action"}})
    assert is_review_current(entry, "fr", standard_version(after, "fr")) is False


# --- needs-review convergence ------------------------------------------------


def test_needs_review_current_true_when_unchanged():
    """A given-up entry whose source/translation/standard are unchanged is
    recognised, so a re-run can skip re-attempting it."""
    cfg = _config()
    std = standard_version(cfg, "fr")
    entry = polib.POEntry(msgid="Activity", msgstr="bad")
    set_needs_review(entry, entry_fingerprint(entry, "fr", std), "stale anchor")
    assert needs_review_current(entry, "fr", std) is True


def test_needs_review_current_false_when_translation_changed():
    """If the entry's translation changed (e.g. a human edited it), it must be
    re-attempted rather than skipped."""
    cfg = _config()
    std = standard_version(cfg, "fr")
    entry = polib.POEntry(msgid="Activity", msgstr="bad")
    set_needs_review(entry, entry_fingerprint(entry, "fr", std), "stale anchor")
    entry.msgstr = "edited by a human"
    assert needs_review_current(entry, "fr", std) is False


def test_needs_review_current_false_after_standard_change():
    cfg_before = _config(glossary={"activity": {"fr": "activité"}})
    std_before = standard_version(cfg_before, "fr")
    entry = polib.POEntry(msgid="Activity", msgstr="bad")
    set_needs_review(entry, entry_fingerprint(entry, "fr", std_before), "reason")

    cfg_after = _config(glossary={"activity": {"fr": "action"}})
    assert needs_review_current(entry, "fr", standard_version(cfg_after, "fr")) is False


def test_needs_review_current_false_when_unmarked():
    entry = polib.POEntry(msgid="Activity", msgstr="ok")
    assert needs_review_current(entry, "fr", standard_version(_config(), "fr")) is False
