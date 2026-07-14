"""
PO file utilities using polib directly.
"""

from collections.abc import Iterable
from pathlib import Path

import polib

from . import config


def get_po_files(language: str) -> list[Path]:
    """Get all .po files for a language, sorted by name."""
    lang_dir = config.LOCALE_DIR / language / "LC_MESSAGES"
    if not lang_dir.exists():
        return []
    return sorted(lang_dir.glob("*.po"))


def load_po_file(path: Path) -> polib.POFile:
    """Load a PO file."""
    return polib.pofile(str(path))


def strip_obsolete(po: polib.POFile) -> int:
    """Remove obsolete (#~) entries from a PO file in-place.

    These are left behind by sphinx-intl update when source strings are
    removed or changed. They serve no purpose in this pipeline.

    Returns the number of entries removed.
    """
    count = len(po.obsolete_entries())
    if count:
        for entry in po.obsolete_entries():
            po.remove(entry)
    return count


def save_po_files(pos: Iterable[polib.POFile]) -> None:
    """Strip obsolete entries from and save each distinct PO file exactly once.

    De-duplicates by object identity, so a caller can collect the same POFile
    repeatedly (e.g. once per modified entry) and still write it a single time.
    This is the shared idiom for the pipeline's "fan-in and save" phases.
    """
    seen: set[int] = set()
    for po in pos:
        if id(po) in seen:
            continue
        seen.add(id(po))
        strip_obsolete(po)
        po.save()


def is_locked(entry: polib.POEntry) -> bool:
    """Check if an entry is locked (has a '# LOCKED' translator comment).

    Locked entries are skipped by the translation pipeline. Users mark entries
    as locked by adding a translator comment starting with LOCKED, optionally
    with a reason: ``# LOCKED: adjusted per user feedback``.
    """
    if not entry.tcomment:
        return False
    return any(line.strip().startswith("LOCKED") for line in entry.tcomment.splitlines())


# Translator-comment marker recording that an entry passed review under a given
# standard. Stored like LOCKED (in entry.tcomment) so it survives
# ``sphinx-intl update``. The value is an opaque fingerprint (see fingerprint.py).
REVIEW_FINGERPRINT_PREFIX = "REVIEWED:"


def get_review_fingerprint(entry: polib.POEntry) -> str | None:
    """Return the stored review fingerprint for an entry, or None if absent."""
    if not entry.tcomment:
        return None
    for line in entry.tcomment.splitlines():
        line = line.strip()
        if line.startswith(REVIEW_FINGERPRINT_PREFIX):
            return line[len(REVIEW_FINGERPRINT_PREFIX):].strip()
    return None


def set_review_fingerprint(entry: polib.POEntry, fingerprint: str) -> bool:
    """Store/update the review fingerprint in the entry's translator comment.

    Other translator-comment lines (e.g. LOCKED) are preserved, but any
    NEEDS-REVIEW marker is dropped — passing review and needing review are
    mutually exclusive. Returns True if the comment actually changed, False if
    it was already current (so callers can avoid rewriting unchanged files).
    """
    kept = [
        line for line in (entry.tcomment or "").splitlines()
        if not line.strip().startswith(REVIEW_FINGERPRINT_PREFIX)
        and not line.strip().startswith(NEEDS_REVIEW_PREFIX)
    ]
    kept.append(f"{REVIEW_FINGERPRINT_PREFIX} {fingerprint}")
    new_tcomment = "\n".join(kept)
    if new_tcomment == (entry.tcomment or ""):
        return False
    entry.tcomment = new_tcomment
    return True


# Translator-comment marker recording that the tool tried to bring an entry up
# to standard but could not — it is left fuzzy (so it renders as the English
# source, never as a known-bad translation) and flagged here for a human. Stored
# like LOCKED/REVIEWED so it survives ``sphinx-intl update``. The value is
# "<fingerprint> <reason>": the fingerprint (see fingerprint.py) lets a re-run
# recognise an entry that will fail identically and skip re-attempting it.
NEEDS_REVIEW_PREFIX = "NEEDS-REVIEW:"


def get_needs_review(entry: polib.POEntry) -> tuple[str, str] | None:
    """Return (fingerprint, reason) from an entry's NEEDS-REVIEW marker, or None.

    The fingerprint is "" if the marker stored no fingerprint.
    """
    if not entry.tcomment:
        return None
    for line in entry.tcomment.splitlines():
        line = line.strip()
        if line.startswith(NEEDS_REVIEW_PREFIX):
            rest = line[len(NEEDS_REVIEW_PREFIX):].strip()
            fingerprint, _, reason = rest.partition(" ")
            return fingerprint.strip(), reason.strip()
    return None


def set_needs_review(entry: polib.POEntry, fingerprint: str, reason: str) -> None:
    """Mark an entry as needing manual review, recording why and a fingerprint.

    Any REVIEWED marker is dropped (the two are mutually exclusive); other
    comment lines (e.g. LOCKED) are preserved.
    """
    kept = [
        line for line in (entry.tcomment or "").splitlines()
        if not line.strip().startswith(NEEDS_REVIEW_PREFIX)
        and not line.strip().startswith(REVIEW_FINGERPRINT_PREFIX)
    ]
    kept.append(f"{NEEDS_REVIEW_PREFIX} {fingerprint} {reason}".rstrip())
    entry.tcomment = "\n".join(kept)


def clear_needs_review(entry: polib.POEntry) -> bool:
    """Remove any NEEDS-REVIEW marker. Returns True if one was present."""
    if not entry.tcomment:
        return False
    kept = [
        line for line in entry.tcomment.splitlines()
        if not line.strip().startswith(NEEDS_REVIEW_PREFIX)
    ]
    if len(kept) == len(entry.tcomment.splitlines()):
        return False
    entry.tcomment = "\n".join(kept)
    return True
