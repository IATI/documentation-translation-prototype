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


def _stems_overlap(expected_word: str, candidate_word: str) -> bool:
    """True if two words share a long common prefix, as a last-resort stem match.

    simplemma's dictionary-based lemmatiser misses some real inflections
    outright rather than mismatching them — e.g. Spanish spelling-change verbs
    (descargar -> descargue, where g becomes gu before e) and gerunds
    (resultar -> resultando) come back unchanged instead of as their
    infinitive. Length-gated prefix overlap catches these without a stemming
    dependency, at the cost of being cruder than real morphology.
    """
    if len(expected_word) < 5 or len(candidate_word) < 5:
        return False
    if abs(len(expected_word) - len(candidate_word)) > 3:
        return False
    common = 0
    for a, b in zip(expected_word, candidate_word):
        if a != b:
            break
        common += 1
    return common >= len(expected_word) - 2


def _expected_term_found(
    expected: str, translation_lower: str, lang: str
) -> bool:
    """Check whether *expected* glossary translation appears in the translation.

    Tries exact substring first (fast path), then lemmatised word-set matching
    so that inflected forms (plurals, conjugations) are recognised, then —
    for single-word expected terms only — a stem-prefix fallback for the
    inflections lemmatisation itself misses.
    """
    if expected.lower() in translation_lower:
        return True

    # Lemmatise the expected multi-word term and the translation, then check
    # that every lemmatised word from the expected term appears in the
    # translation's lemmatised words.
    expected_lemmas = _lemmatized_words(expected, lang)
    translation_lemmas = _lemmatized_words(translation_lower, lang)
    if expected_lemmas and expected_lemmas.issubset(translation_lemmas):
        return True

    if len(expected_lemmas) == 1:
        expected_word = next(iter(expected_lemmas))
        translation_words = re.findall(r'\w+', translation_lower)
        if any(_stems_overlap(expected_word, w) for w in translation_words):
            return True

    return False


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
                "expected": expected_display,
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


# A translation from English into FR/ES/PT normally EXPANDS the text (typically
# +10-30%). One that fails to grow, or that has fewer sentences than the source,
# is a cheap, high-recall signal that a clause may have been dropped. These
# thresholds only decide whether an entry is worth an (expensive) LLM
# completeness check — they never flag an entry on their own, so they are
# deliberately loose and biased toward recall.
_TRUNCATION_LENGTH_RATIO = 0.9
_MIN_SOURCE_LEN_FOR_TRUNCATION = 40


def is_truncation_suspect(source: str, translation: str) -> bool:
    """True if the translation might be missing part of the source's meaning.

    A recall-oriented pre-filter for the LLM completeness check: cheap, pure, and
    intentionally over-inclusive. Short sources are ignored (headings, UI labels
    and glossary terms legitimately stay short and often shrink). For longer
    prose, a translation that does not expand on the source, or that contains
    fewer sentence-ending marks, is treated as a suspect worth an LLM look.
    """
    if len(source) < _MIN_SOURCE_LEN_FOR_TRUNCATION:
        return False
    if len(translation) < len(source) * _TRUNCATION_LENGTH_RATIO:
        return True
    src_sentences = len(re.findall(r'[.!?](?:\s|$)', source))
    trans_sentences = len(re.findall(r'[.!?](?:\s|$)', translation))
    return src_sentences >= 2 and trans_sentences < src_sentences


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


def _escaped_number_match(text: str) -> re.Match | None:
    r"""Match a leading escaped item number prefix (e.g. ``\6. `` at the start)."""
    return re.match(r'^\\(\d+)\.\s*', text)


def _escaped_number_prefix(text: str) -> str | None:
    r"""Return the leading escaped item number (e.g. '6' from '\6. ...').

    Sphinx sources escape a literal leading number as ``\6.`` to stop
    auto-numbering. The number identifies the item and must match the source;
    a stale number is another carry-over symptom after renumbering.
    """
    m = _escaped_number_match(text)
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


def fix_numbered_prefix(source: str, translation: str) -> str | None:
    r"""Auto-fix a stale or missing escaped item number prefix (``\6.``).

    The escaped number is a mechanical carry-over from the source (a Sphinx
    auto-numbering escape) — it is always correct to copy verbatim from the
    source, never something for the LLM to infer. Returns the fixed
    translation, or None if no fix is needed or the source has no such prefix.
    """
    src_match = _escaped_number_match(source)
    if src_match is None:
        return None

    trans_match = _escaped_number_match(translation)
    if trans_match is not None and trans_match.group(1) == src_match.group(1):
        return None

    rest = translation[trans_match.end():] if trans_match else translation
    return source[:src_match.end()] + rest


# JSON response envelopes the model is asked to reply with, e.g.
# {"translation": "..."} or {"status": "revised", "revisions": [...]}. When the
# model double-wraps its answer (nesting JSON inside the "translation" value) or a
# parse falls through to the wrong field, that envelope can be written verbatim as
# the msgstr. None of these tokens belong in translated prose.
_ENVELOPE_MARKERS = (
    '{"translation"',
    "{'translation'",
    '"translation":',
    '"revised":',
    '"revisions":',
    '"source_span":',
)


def check_no_llm_artifacts(source: str, translation: str) -> list[dict]:
    r"""Flag raw LLM/JSON machinery that leaked into the translation text.

    Catches two recurring corruptions that otherwise pass every other check and
    get stamped REVIEWED:
    - JSON response envelopes ({"translation": ...}, {"revised": ...}) written
      verbatim into the msgstr when the model double-wrapped its answer.
    - Literal escape sequences (``\n``, ``\t``, ``\r`` — often as ``\n0``) left
      behind by mangled continuation lines. A real newline decodes to an actual
      character, so a *literal* backslash-n in the text is an artifact, not
      content. Sequences already present in the source are ignored (the source
      is the authority on which literal backslashes are legitimate).
    """
    issues = []

    for marker in _ENVELOPE_MARKERS:
        if marker in translation:
            issues.append({
                "type": "llm_artifact",
                "description": (
                    f"Translation contains a leaked LLM/JSON envelope "
                    f"('{marker}') — raw model output was written as the translation"
                ),
            })
            break  # one report is enough; these markers co-occur

    literal_escapes = set(re.findall(r'\\[ntr]', translation))
    literal_escapes -= set(re.findall(r'\\[ntr]', source))
    if literal_escapes:
        issues.append({
            "type": "llm_artifact",
            "description": (
                "Translation contains literal escape sequences "
                f"({', '.join(sorted(literal_escapes))}) absent from the source "
                "— likely a mangled continuation line"
            ),
        })

    return issues


def _iati_urls(text: str) -> list[str]:
    """Return IATI-domain URLs in *text*, in order of appearance."""
    return [
        u for u in re.findall(r'https?://[^\s>)"`\']+', text)
        if any(domain in u for domain in TRANSLATABLE_URL_DOMAINS)
    ]


def _normalize_lang_code(url: str, language: str) -> str:
    """Collapse a URL's /en/ or /{language}/ segment to a placeholder.

    Lets a source URL and its language-adjusted translation compare equal, so the
    only remaining differences are genuine corruptions of the link text.
    """
    return url.replace("/en/", "/*LANG*/").replace(f"/{language}/", "/*LANG*/")


def check_url_integrity(source: str, translation: str, language: str) -> list[dict]:
    """Flag IATI-domain URLs in the translation with no matching source URL.

    URLs are not translatable: aside from the language-code segment, every IATI
    URL in the translation must be copied verbatim from the source. A URL with no
    source counterpart means the model rewrote text *inside* the link — most
    often applying a glossary/acronym rule (e.g. IATI->IITA in French prose) to
    the path, which 404s. Only IATI domains are checked, where URLs are always
    verbatim carry-overs from the source.
    """
    source_norm = {_normalize_lang_code(u, language) for u in _iati_urls(source)}
    if not source_norm:
        return []

    issues = []
    for url in _iati_urls(translation):
        if _normalize_lang_code(url, language) not in source_norm:
            issues.append({
                "type": "url_corruption",
                "description": (
                    f"URL has no match in the source (text was altered inside the "
                    f"link): {url}"
                ),
                "url": url,
            })
    return issues


def fix_url_integrity(source: str, translation: str, language: str) -> str | None:
    """Auto-fix IATI-domain URLs whose text was altered inside the link.

    When source and translation carry the same number of IATI URLs, each
    translation URL is replaced (in appearance order) with the corresponding
    source URL, language-code adjusted to the target — restoring links the model
    mangled (e.g. IATI->IITA in the path) without touching surrounding prose. If
    the counts differ the URLs can't be matched unambiguously, so it is left for
    the LLM-correction / manual-review path. Returns the fixed translation, or
    None when nothing needs (or can safely) be fixed.
    """
    source_urls = _iati_urls(source)
    trans_urls = _iati_urls(translation)
    if not source_urls or len(source_urls) != len(trans_urls):
        return None

    fixed_sources = [fix_url_language_codes(u, language) for u in source_urls]

    # Nothing to do if every translation URL already matches its source.
    if all(
        _normalize_lang_code(t, language) == _normalize_lang_code(s, language)
        for t, s in zip(trans_urls, fixed_sources)
    ):
        return None

    replacements = iter(fixed_sources)

    def _replace(match: re.Match) -> str:
        url = match.group(0)
        if any(domain in url for domain in TRANSLATABLE_URL_DOMAINS):
            return next(replacements)
        return url

    result = re.sub(r'https?://[^\s>)"`\']+', _replace, translation)
    return result if result != translation else None


def check_bracket_balance(source: str, translation: str) -> list[dict]:
    """Flag a translation whose parentheses/brackets are unbalanced when the source's are.

    A source with matched delimiters that becomes unbalanced in translation is a
    reliable symptom of a leaked fragment — e.g. a reviewer's note
    ("...flagged for consistency)") appended into the published text. Genuine
    restructuring keeps delimiters balanced, so this leaves it alone.
    """
    issues = []
    for open_ch, close_ch, label in [("(", ")", "parentheses"), ("[", "]", "brackets")]:
        source_balanced = source.count(open_ch) == source.count(close_ch)
        trans_balanced = translation.count(open_ch) == translation.count(close_ch)
        if source_balanced and not trans_balanced:
            issues.append({
                "type": "bracket_balance",
                "description": (
                    f"Unbalanced {label}: source is balanced but translation has "
                    f"{translation.count(open_ch)} '{open_ch}' and "
                    f"{translation.count(close_ch)} '{close_ch}' — possible leaked fragment"
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
    issues.extend(check_no_llm_artifacts(source, translation))
    issues.extend(check_url_language_codes(source, translation, language))
    issues.extend(check_url_integrity(source, translation, language))
    issues.extend(check_length_ratio(source, translation))
    issues.extend(check_formatting_preserved(source, translation))
    issues.extend(check_bracket_balance(source, translation))
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
    - Stale/missing escaped item number prefixes

    Returns list of descriptions of fixes applied.
    """
    fixes = []

    # Fix URL language codes
    fixed = fix_url_language_codes(entry.msgstr, language)
    if fixed != entry.msgstr:
        entry.msgstr = fixed
        fixes.append(f"Fixed URL language codes (/en/ -> /{language}/)")

    # Restore IATI URLs whose inner text was altered (e.g. IATI -> IITA in path)
    fixed = fix_url_integrity(entry.msgid, entry.msgstr, language)
    if fixed is not None:
        entry.msgstr = fixed
        fixes.append("Restored corrupted text inside IATI URL(s)")

    # Fix missing list prefixes
    fixed = fix_list_prefix(entry.msgid, entry.msgstr)
    if fixed is not None:
        prefix = _extract_list_prefix(entry.msgid).rstrip()
        entry.msgstr = fixed
        fixes.append(f"Fixed missing list prefix '{prefix}'")

    # Fix stale/missing escaped item number prefixes (e.g. "\6.")
    fixed = fix_numbered_prefix(entry.msgid, entry.msgstr)
    if fixed is not None:
        entry.msgstr = fixed
        fixes.append("Fixed escaped item number prefix")

    return fixes
