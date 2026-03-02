"""
Deterministic post-processing checks for translations.

These checks verify mechanically verifiable rules (URL language codes,
glossary term preservation, length ratios) without relying on LLM judgment.
"""

import re

import polib

from .config import TranslationConfig


# Domains where /en/ in the path should be replaced with /{lang}/
TRANSLATABLE_URL_DOMAINS = [
    "iatistandard.org",
    "docs.publisher.iatistandard.org",
    "account.iatistandard.org",
]

# Reasonable length ratio bounds (translation vs source)
MIN_LENGTH_RATIO = 0.4
MAX_LENGTH_RATIO = 2.5


def check_url_language_codes(
    source: str, translation: str, language: str
) -> list[dict]:
    """Check that URLs with /en/ paths are updated to /{language}/.

    Only checks URLs on known IATI domains where language codes are expected.
    Returns list of issues with 'description' and optionally 'fixed' text.
    """
    issues = []

    source_urls = re.findall(r'https?://[^\s>)"`\']+', source)
    translation_urls = re.findall(r'https?://[^\s>)"`\']+', translation)

    for url in translation_urls:
        # Only check URLs on domains we know have language paths
        is_translatable = any(domain in url for domain in TRANSLATABLE_URL_DOMAINS)
        if not is_translatable:
            continue

        # Check if this URL still has /en/ when it should have /{language}/
        if f"/{language}/" not in url and "/en/" in url:
            # Verify the source also had /en/ (not some other context)
            source_had_en = any("/en/" in s_url for s_url in source_urls)
            if source_had_en:
                issues.append({
                    "type": "url_language_code",
                    "description": f"URL still has /en/ instead of /{language}/: {url}",
                    "url": url,
                })

    return issues


def fix_url_language_codes(translation: str, language: str) -> str:
    """Auto-fix URL language codes in translation by replacing /en/ with /{language}/.

    Only fixes URLs on known IATI domains.
    """
    def replace_url_lang(match: re.Match) -> str:
        url = match.group(0)
        is_translatable = any(domain in url for domain in TRANSLATABLE_URL_DOMAINS)
        if is_translatable:
            return url.replace("/en/", f"/{language}/")
        return url

    return re.sub(r'https?://[^\s>)"`\']+', replace_url_lang, translation)


def check_glossary_terms(
    source: str,
    translation: str,
    language: str = "",
    config: TranslationConfig | None = None,
) -> list[dict]:
    """Check that glossary terms appearing in source are preserved in translation.

    For each glossary term found in the source, verifies that either the
    English term itself or its known translation for this language appears
    in the translation.

    Returns list of issues with 'description'.
    """
    if config is None:
        return []

    issues = []

    for term, translations in config.glossary.items():
        if term not in source:
            continue

        # The term must appear verbatim OR its glossary translation
        # for this language must appear.
        if term in translation:
            continue

        lang_translation = translations.get(language, "")
        if lang_translation and lang_translation in translation:
            continue

        issues.append({
            "type": "glossary_term",
            "description": f"Glossary term '{term}' was modified in translation",
            "name": term,
        })

    return issues


def check_length_ratio(source: str, translation: str) -> list[dict]:
    """Check that translation length is within reasonable bounds.

    Very short translations may indicate truncation/summarization.
    Very long translations may indicate added content.
    Returns list of issues with 'description'.
    """
    if len(source) < 20:
        # Too short to meaningfully check ratios
        return []

    ratio = len(translation) / len(source)
    issues = []

    if ratio < MIN_LENGTH_RATIO:
        issues.append({
            "type": "length_ratio",
            "description": (
                f"Translation is suspiciously short "
                f"({len(translation)} chars vs {len(source)} source chars, "
                f"ratio {ratio:.2f})"
            ),
        })
    elif ratio > MAX_LENGTH_RATIO:
        issues.append({
            "type": "length_ratio",
            "description": (
                f"Translation is suspiciously long "
                f"({len(translation)} chars vs {len(source)} source chars, "
                f"ratio {ratio:.2f})"
            ),
        })

    return issues


def check_formatting_preserved(source: str, translation: str) -> list[dict]:
    """Check that key RST/Markdown formatting is preserved.

    Checks for matching counts of formatting markers.
    Returns list of issues with 'description'.
    """
    issues = []

    checks = [
        ("**", "bold markers (**)"),
        ("``", "inline code markers (``)"),
    ]

    for marker, label in checks:
        source_count = source.count(marker)
        trans_count = translation.count(marker)
        if source_count != trans_count:
            issues.append({
                "type": "formatting",
                "description": (
                    f"Mismatched {label}: "
                    f"{source_count} in source vs {trans_count} in translation"
                ),
            })

    # Check RST link syntax: `text <url>`_
    source_rst_links = re.findall(r'`[^`]+<[^>]+>`_', source)
    trans_rst_links = re.findall(r'`[^`]+<[^>]+>`_', translation)
    if len(source_rst_links) != len(trans_rst_links):
        issues.append({
            "type": "formatting",
            "description": (
                f"Mismatched RST links: "
                f"{len(source_rst_links)} in source vs {len(trans_rst_links)} in translation"
            ),
        })

    return issues


def run_all_checks(
    entry: polib.POEntry,
    language: str,
    config: TranslationConfig | None = None,
) -> list[dict]:
    """Run all deterministic checks on a single translation entry.

    Returns list of issue dicts, each with at minimum 'type' and 'description'.
    """
    source = entry.msgid
    translation = entry.msgstr

    if not source or not translation:
        return []

    issues = []
    issues.extend(check_url_language_codes(source, translation, language))
    issues.extend(check_length_ratio(source, translation))
    issues.extend(check_formatting_preserved(source, translation))

    return issues


def validate_revision(
    source: str,
    original: str,
    revised: str,
    language: str,
    config: TranslationConfig | None = None,
) -> list[dict]:
    """Check whether a proposed revision is safe to apply.

    Compares the deterministic-check results of the *original* translation
    against those of the *revised* translation.  Returns a list of issues
    that the revision would **introduce** (i.e. issues present in the
    revised text but not in the original).  An empty list means the
    revision is safe.
    """
    # Build temporary POEntry objects for checking
    orig_entry = polib.POEntry(msgid=source, msgstr=original)
    rev_entry = polib.POEntry(msgid=source, msgstr=revised)

    orig_issues = {
        (i["type"], i.get("name", ""), i.get("url", ""))
        for i in run_all_checks(orig_entry, language, config)
    }
    rev_issues = run_all_checks(rev_entry, language, config)

    new_issues = [
        i for i in rev_issues
        if (i["type"], i.get("name", ""), i.get("url", "")) not in orig_issues
    ]
    return new_issues


def auto_fix_entry(
    entry: polib.POEntry,
    language: str,
) -> list[str]:
    """Apply auto-fixes to a translation entry where possible.

    Currently auto-fixes:
    - URL language codes

    Returns list of descriptions of fixes applied.
    """
    fixes = []

    # Fix URL language codes
    fixed = fix_url_language_codes(entry.msgstr, language)
    if fixed != entry.msgstr:
        entry.msgstr = fixed
        fixes.append(f"Fixed URL language codes (/en/ -> /{language}/)")

    return fixes
