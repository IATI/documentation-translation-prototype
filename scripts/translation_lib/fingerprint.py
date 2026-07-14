"""
Review fingerprints: decide whether a translation still reflects the current
standard, so re-runs skip re-reviewing settled entries instead of churning them.

An entry's fingerprint combines its source + translation with a "standard
version" derived from the glossary, guidelines, few-shot examples, UI terms,
and formatting rules. When any of those evolve, the standard version changes,
every stored fingerprint goes stale, and the affected translations are
re-reviewed on the next run — so improvements to the standard propagate
automatically without the user having to ask.
"""

import hashlib
import json
import subprocess
from functools import lru_cache

import polib

from .config import TOOL_ROOT, TranslationConfig
from .po_utils import get_needs_review, get_review_fingerprint
from .prompts import FORMATTING_RULES


@lru_cache(maxsize=1)
def code_version() -> str:
    """Identify the currently-running tool code, for cache invalidation.

    Returns the tool repo's HEAD commit (short), suffixed with '+dirty' when
    tracked files under scripts/ differ from that commit. Folding this into
    standard_version() means any change to the translation logic (quality.py,
    checks.py, prompts.py, ...) automatically invalidates stored review and
    NEEDS-REVIEW fingerprints on the next run — so a fix to previously-unfixable
    entries actually reaches them, with no manual version bump to forget.

    The tool always runs from a git checkout, so git is assumed present. Cached:
    git is only consulted once per process.
    """
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=str(TOOL_ROOT), capture_output=True, text=True, check=True,
    ).stdout.strip()

    # Tracked-file changes under scripts/ mean the running code differs from the
    # commit; scoping to scripts/ avoids false "dirty" from logs or build output.
    dirty = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", "scripts"],
        cwd=str(TOOL_ROOT), capture_output=True,
    ).returncode != 0
    return f"{commit}+dirty" if dirty else commit


def standard_version(config: TranslationConfig, language: str) -> str:
    """Short hash of everything that defines the translation standard for a language.

    Folds in the formatting rules, project guidelines, the language-specific
    glossary, UI terms, few-shot examples, and the running code version (the
    tool's git commit). Changing any of these changes the returned value, which
    invalidates stored review fingerprints and forces the affected translations
    to be re-reviewed.
    """
    payload = {
        "code_version": code_version(),
        "formatting_rules": FORMATTING_RULES,
        "notes": (config.notes or "").strip(),
        "glossary": {
            term: trans.get(language, "")
            for term, trans in sorted(config.glossary.items())
        },
        "ui_terms": {
            term: trans.get(language, "")
            for term, trans in sorted(config.ui_terms.items())
        },
        "examples": sorted(
            (ex.get("source", ""), ex.get("translation", ""))
            for ex in config.few_shot_examples
            if ex.get("target_language") == language
        ),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def entry_fingerprint(
    entry: polib.POEntry, language: str, std_version: str
) -> str:
    """Fingerprint identifying this exact (source, translation) under a standard."""
    blob = "\x00".join([language, std_version, entry.msgid, entry.msgstr or ""])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def is_review_current(
    entry: polib.POEntry, language: str, std_version: str
) -> bool:
    """True if the entry already passed review under the current standard.

    Such entries can be skipped by the LLM review phases — their stored
    fingerprint matches their current source, translation, and standard.
    """
    stored = get_review_fingerprint(entry)
    return stored is not None and stored == entry_fingerprint(
        entry, language, std_version
    )


def needs_review_current(
    entry: polib.POEntry, language: str, std_version: str
) -> bool:
    """True if the entry was already given up on under the current standard.

    Such an entry has an unchanged source, translation, and standard since the
    tool last failed to bring it up to standard — re-attempting it would fail
    identically (translation is deterministic), so a re-run can skip it and
    leave it flagged for a human instead of burning tokens.
    """
    nr = get_needs_review(entry)
    if nr is None:
        return False
    stored_fp, _reason = nr
    return bool(stored_fp) and stored_fp == entry_fingerprint(
        entry, language, std_version
    )
