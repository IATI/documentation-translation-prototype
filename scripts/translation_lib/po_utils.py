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
