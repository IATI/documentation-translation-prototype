"""
Deterministic post-processing checks for translations.

These checks verify mechanically verifiable rules (URL language codes,
glossary term preservation, length ratios) without relying on LLM judgment.
"""

import re
from collections import defaultdict

import polib
import simplemma

from .config import TranslationConfig


# Mapping from our short language codes to simplemma language codes
_SIMPLEMMA_LANG = {"fr": "fr", "es": "es", "pt": "pt", "en": "en"}


def _lemmatized_words(text: str, lang: str) -> set[str]:
    """Return the set of lemmatised words in *text* for the given language."""
    slang = _SIMPLEMMA_LANG.get(lang, lang)
    return {
        simplemma.lemmatize(w, lang=slang, greedy=True)
        for w in re.findall(r'\w+', text.lower())
    }


def _expected_term_found(
    expected: str, translation_lower: str, lang: str
) -> bool:
    """Check whether *expected* glossary translation appears in the translation.

    Tries exact substring first (fast path), then falls back to lemmatised
    word-set matching so that inflected forms (plurals, conjugations) are
    recognised.
    """
    if expected.lower() in translation_lower:
        return True

    # Lemmatise the expected multi-word term and the translation, then check
    # that every lemmatised word from the expected term appears in the
    # translation's lemmatised words.
    expected_lemmas = _lemmatized_words(expected, lang)
    translation_lemmas = _lemmatized_words(translation_lower, lang)
    return bool(expected_lemmas) and expected_lemmas.issubset(translation_lemmas)


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


def _parse_glossary_key(key: str) -> tuple[str, str]:
    """Parse a glossary key like 'download (verb)' into ('download', 'verb').

    Returns (term, pos) where pos is empty string if not present.
    """
    if key.endswith(")") and " (" in key:
        term, pos = key.rsplit(" (", 1)
        return term, pos[:-1]
    return key, ""


def check_glossary_terms(
    source: str,
    translation: str,
    language: str = "",
    config: TranslationConfig | None = None,
) -> list[dict]:
    """Check that glossary terms appearing in source are used in translation.

    For each glossary term found in the source (case-insensitive, word-boundary
    matched), verifies that the expected translation for this language appears
    in the translation (also case-insensitive, checking for the stem to allow
    inflections like plurals and articles).

    Uses positional overlap analysis to avoid false positives when a shorter
    term only appears as part of a longer matched term in the source text
    (e.g. "In IATI" inside "In IATI Publisher", "file" inside "profile").

    When a term has multiple POS entries (e.g. download verb/noun), the check
    passes if any expected translation variant is found.

    Returns list of issues with 'description'.
    """
    if config is None:
        return []

    source_lower = source.lower()
    translation_lower = translation.lower()

    # Find all glossary terms present in source (word-boundary matching)
    matched: list[tuple[str, str]] = []  # (term, expected_translation)
    for key, translations in config.glossary.items():
        lang_translation = translations.get(language, "")
        if not lang_translation:
            continue
        term, _ = _parse_glossary_key(key)
        pattern = r'\b' + re.escape(term.lower()) + r'\b'
        if re.search(pattern, source_lower):
            matched.append((term, lang_translation))

    # Find positions of each term in source for overlap analysis
    term_positions: dict[int, list[tuple[int, int]]] = {}
    for i, (term, _) in enumerate(matched):
        pattern = r'\b' + re.escape(term.lower()) + r'\b'
        term_positions[i] = [
            (m.start(), m.end())
            for m in re.finditer(pattern, source_lower)
        ]

    # Remove terms where ALL occurrences overlap with a strictly longer
    # matched term (e.g. "In IATI" suppressed when only appearing inside
    # "In IATI Publisher")
    to_remove: set[int] = set()
    for i, (term_i, _) in enumerate(matched):
        positions_i = term_positions.get(i, [])
        if not positions_i:
            continue
        all_overlapped = True
        for start_i, end_i in positions_i:
            overlapped = False
            for j, (term_j, _) in enumerate(matched):
                if j == i or len(term_j) <= len(term_i):
                    continue
                for start_j, end_j in term_positions.get(j, []):
                    if start_j < end_i and end_j > start_i:
                        overlapped = True
                        break
                if overlapped:
                    break
            if not overlapped:
                all_overlapped = False
                break
        if all_overlapped:
            to_remove.add(i)

    terms_to_check = [m for i, m in enumerate(matched) if i not in to_remove]

    # Group by term to handle multiple POS entries (e.g. download verb/noun).
    # If ANY expected translation for a term is found, skip it entirely.
    term_groups: defaultdict[str, list[tuple[str, str]]] = defaultdict(list)
    for term, expected in terms_to_check:
        term_groups[term.lower()].append((term, expected))

    issues = []
    for term_lower, entries in term_groups.items():
        any_found = False
        for _, expected in entries:
            if _expected_term_found(expected, translation_lower, language):
                any_found = True
                break
        # Also accept the English term appearing verbatim
        if not any_found and term_lower in translation_lower:
            any_found = True

        if not any_found:
            original_term = entries[0][0]
            expected_display = entries[0][1]
            issues.append({
                "type": "glossary_term",
                "description": (
                    f"Glossary term '{original_term}' should be translated as "
                    f"'{expected_display}' but was not found in translation"
                ),
                "name": original_term,
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


def _extract_list_prefix(text: str) -> str | None:
    """Extract a numbered/lettered list prefix from the start of text.

    Recognises patterns like: "1)", "2.", "(a)", "(3)", "a)", "iv." etc.
    Returns the prefix string (e.g. "1) ") including trailing whitespace,
    or None if not found.
    """
    m = re.match(r'^(\(?[0-9a-zA-Z]+[).])\s*', text)
    return m.group(0) if m else None


def check_list_prefix(source: str, translation: str) -> list[dict]:
    """Check that numbered/lettered list prefixes are preserved.

    If the source starts with a list prefix (e.g. "1) ", "a. "),
    the translation must start with the same prefix.
    """
    prefix = _extract_list_prefix(source)
    if prefix is None:
        return []

    trans_prefix = _extract_list_prefix(translation)
    if trans_prefix is not None and trans_prefix.rstrip() == prefix.rstrip():
        return []

    return [{
        "type": "list_prefix",
        "description": (
            f"Source starts with list prefix '{prefix.rstrip()}' "
            f"but translation does not"
        ),
        "prefix": prefix.rstrip(),
    }]


def fix_list_prefix(source: str, translation: str) -> str | None:
    """Auto-fix a missing list prefix by prepending it to the translation.

    Returns the fixed translation, or None if no fix was needed.
    """
    prefix = _extract_list_prefix(source)
    if prefix is None:
        return None

    trans_prefix = _extract_list_prefix(translation)
    if trans_prefix is not None and trans_prefix.rstrip() == prefix.rstrip():
        return None

    # Prepend the source prefix to the translation
    return prefix + translation


def _count_italic_spans(text: str) -> int:
    """Count *italic* spans that aren't part of **bold** markers."""
    # Remove **bold** spans first so their asterisks don't confuse us
    stripped = re.sub(r'\*\*[^*]+\*\*', '', text)
    # Count genuine *italic* spans
    return len(re.findall(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', stripped))


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

    # Check for italic spans (*text*) that aren't part of **bold**
    source_italics = _count_italic_spans(source)
    trans_italics = _count_italic_spans(translation)
    if source_italics != trans_italics:
        issues.append({
            "type": "formatting",
            "description": (
                f"Mismatched italic spans (*...*): "
                f"{source_italics} in source vs {trans_italics} in translation"
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


# RST cross-reference roles (e.g. :ref:`Visible text <target>`) carry a
# <target> anchor that points at a labelled location elsewhere in the docs.
# The anchor is NOT translatable text — it must be copied verbatim. Translation
# memory carries this over from near-matches, so when an FAQ list is renumbered
# the visible text changes but a stale anchor (and number) can be left behind.
_REF_ROLE_RE = re.compile(r':[a-zA-Z][\w+-]*:`([^`]*)`')


def _ref_targets(text: str) -> list[str]:
    """Return the sorted list of cross-reference anchors used in *text*.

    Extracts the ``<target>`` from each ``:role:`text <target>``` construct.
    Sorted so the comparison is order-insensitive (a multiset compare): the
    same set of anchors counts as preserved even if clauses are reordered.
    """
    targets: list[str] = []
    for content in _REF_ROLE_RE.findall(text):
        m = re.search(r'<([^>]+)>', content)
        if m:
            targets.append(m.group(1).strip())
    return sorted(targets)


def check_ref_targets(source: str, translation: str) -> list[dict]:
    """Check that RST cross-reference anchors are preserved verbatim.

    A mismatch means the translation points at a different doc location than
    the source — the classic symptom of translation-memory carry-over after a
    list/anchor was renumbered.
    """
    src = _ref_targets(source)
    trans = _ref_targets(translation)
    if src == trans:
        return []
    return [{
        "type": "ref_target",
        "description": (
            f"Cross-reference anchor mismatch: source points to "
            f"{src or '(none)'} but translation points to {trans or '(none)'}"
        ),
    }]


def _escaped_number_prefix(text: str) -> str | None:
    r"""Return the leading escaped item number (e.g. '6' from '\6. ...').

    Sphinx sources escape a literal leading number as ``\6.`` to stop
    auto-numbering. The number identifies the item and must match the source;
    a stale number is another carry-over symptom after renumbering.
    """
    m = re.match(r'^\\(\d+)\.', text)
    return m.group(1) if m else None


def check_numbered_prefix(source: str, translation: str) -> list[dict]:
    r"""Check that a leading escaped item number (``\6.``) matches the source."""
    src_num = _escaped_number_prefix(source)
    if src_num is None:
        return []

    trans_num = _escaped_number_prefix(translation)
    if trans_num == src_num:
        return []

    trans_display = f"\\{trans_num}." if trans_num else "(none)"
    return [{
        "type": "numbered_prefix",
        "description": (
            f"Item number mismatch: source starts with '\\{src_num}.' "
            f"but translation starts with {trans_display}"
        ),
    }]


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
    issues.extend(check_list_prefix(source, translation))
    issues.extend(check_ref_targets(source, translation))
    issues.extend(check_numbered_prefix(source, translation))
    issues.extend(check_glossary_terms(source, translation, language, config))

    return issues


def _issue_key(issue: dict) -> tuple[str, str]:
    """Stable identity for an issue, used to compare two check results.

    Variable numbers (counts, ratios) are stripped from the description so that
    issues of the same *kind* compare equal — e.g. a translation that is still
    "suspiciously long" after revision is not mistaken for a newly introduced
    problem. Distinct kinds (bold vs italic mismatch, different glossary terms,
    different URLs) keep distinct keys because their descriptions differ in
    non-numeric text.
    """
    return (issue["type"], re.sub(r"\d+", "", issue.get("description", "")))


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

    orig_issues = {_issue_key(i) for i in run_all_checks(orig_entry, language, config)}
    rev_issues = run_all_checks(rev_entry, language, config)

    new_issues = [i for i in rev_issues if _issue_key(i) not in orig_issues]
    return new_issues


def auto_fix_entry(
    entry: polib.POEntry,
    language: str,
) -> list[str]:
    """Apply auto-fixes to a translation entry where possible.

    Currently auto-fixes:
    - URL language codes
    - Missing list prefixes

    Returns list of descriptions of fixes applied.
    """
    fixes = []

    # Fix URL language codes
    fixed = fix_url_language_codes(entry.msgstr, language)
    if fixed != entry.msgstr:
        entry.msgstr = fixed
        fixes.append(f"Fixed URL language codes (/en/ -> /{language}/)")

    # Fix missing list prefixes
    fixed = fix_list_prefix(entry.msgid, entry.msgstr)
    if fixed is not None:
        prefix = _extract_list_prefix(entry.msgid).rstrip()
        entry.msgstr = fixed
        fixes.append(f"Fixed missing list prefix '{prefix}'")

    return fixes
