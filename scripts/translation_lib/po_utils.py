"""
PO file utilities using polib directly.
"""

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

    Other translator-comment lines (e.g. LOCKED) are preserved. Returns True if
    the comment actually changed, False if the fingerprint was already current
    (so callers can avoid rewriting unchanged files).
    """
    kept = [
        line for line in (entry.tcomment or "").splitlines()
        if not line.strip().startswith(REVIEW_FINGERPRINT_PREFIX)
    ]
    kept.append(f"{REVIEW_FINGERPRINT_PREFIX} {fingerprint}")
    new_tcomment = "\n".join(kept)
    if new_tcomment == (entry.tcomment or ""):
        return False
    entry.tcomment = new_tcomment
    return True
