"""
Display formatting helpers for translation CLI output.
"""

import polib


def truncate(text: str, length: int = 60) -> str:
    """Truncate text for display."""
    text = text.replace("\n", " ")
    if len(text) > length:
        return text[:length] + "..."
    return text


def format_diff(old: str, new: str, context: int = 25) -> str:
    """Show a focused diff between old and new strings.

    Finds the first point of difference and shows context around it.
    If strings are short enough, shows them in full.
    """
    old_flat = old.replace("\n", " ")
    new_flat = new.replace("\n", " ")

    # Short enough to show in full
    if len(old_flat) <= 70 and len(new_flat) <= 70:
        return f'"{old_flat}" -> "{new_flat}"'

    # Find first difference
    diff_pos = 0
    for i, (a, b) in enumerate(zip(old_flat, new_flat)):
        if a != b:
            diff_pos = i
            break
    else:
        # One is a prefix of the other (length difference)
        diff_pos = min(len(old_flat), len(new_flat))

    # Extract windows around the difference
    start = max(0, diff_pos - context)
    old_end = min(len(old_flat), diff_pos + context)
    new_end = min(len(new_flat), diff_pos + context)

    old_snippet = old_flat[start:old_end]
    new_snippet = new_flat[start:new_end]

    prefix = "..." if start > 0 else ""
    old_suffix = "..." if old_end < len(old_flat) else ""
    new_suffix = "..." if new_end < len(new_flat) else ""

    return f'"{prefix}{old_snippet}{old_suffix}" -> "{prefix}{new_snippet}{new_suffix}"'


def location(entry: polib.POEntry) -> str:
    """Get a short location string from a PO entry."""
    if entry.occurrences:
        ref_file, ref_line = entry.occurrences[0]
        name = ref_file.split("/")[-1] if "/" in ref_file else ref_file
        return f"{name}:{ref_line}"
    return ""
