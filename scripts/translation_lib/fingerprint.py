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

import polib

from .config import TranslationConfig
from .po_utils import get_needs_review, get_review_fingerprint
from .prompts import FORMATTING_RULES


def standard_version(config: TranslationConfig, language: str) -> str:
    """Short hash of everything that defines the translation standard for a language.

    Folds in the formatting rules, project guidelines, and the language-specific
    glossary, UI terms, and few-shot examples. Changing any of these changes the
    returned value, which invalidates stored review fingerprints and forces the
    affected translations to be re-reviewed.
    """
    payload = {
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
