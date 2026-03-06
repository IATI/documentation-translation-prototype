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


def is_locked(entry: polib.POEntry) -> bool:
    """Check if an entry is locked (has a '# LOCKED' translator comment).

    Locked entries are skipped by the translation pipeline. Users mark entries
    as locked by adding a translator comment starting with LOCKED, optionally
    with a reason: ``# LOCKED: adjusted per user feedback``.
    """
    if not entry.tcomment:
        return False
    return any(line.strip().startswith("LOCKED") for line in entry.tcomment.splitlines())
