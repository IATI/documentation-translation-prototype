"""
LLM prompt builders for translation and review.
"""

import re

from .config import LANGUAGE_NAMES, TranslationConfig


# -----------------------------------------------------------------------------
# Shared rules (single source of truth)
# -----------------------------------------------------------------------------

FORMATTING_RULES = """
- Translate the COMPLETE text — every sentence, every clause. Never summarize or truncate.
- Preserve ALL reStructuredText and Markdown formatting (links, bold, italics, code blocks).
- Do NOT add formatting (bold, italic) that is not in the English source.
- Do NOT add content that is not in the English source (e.g. captions, labels, figure numbers, explanatory notes).
- Do NOT remove formatting that IS in the English source.
- Copy URLs exactly — never retype or modify them. The ONE exception: change language codes in URL paths (e.g. /en/ -> /fr/).
- Do NOT translate text inside backticks (code/commands).
- Preserve numbered/lettered list prefixes exactly (e.g. "1)", "2.", "(a)") — do not drop or renumber them.
- Use glossary and UI terms exactly as specified.
""".strip()


# Error categories the per-file reviewer is allowed to cite. A proposed
# revision that does not name one of these — with a quoted source span as
# evidence — is discarded in review.py. This is the main brake on stylistic
# churn: the reviewer must justify a change as a concrete error, not a
# preference.
REVIEW_ERROR_CATEGORIES = ("incomplete", "mistranslation", "glossary", "register")


# -----------------------------------------------------------------------------
# Shared Utilities
# -----------------------------------------------------------------------------


def _get_language_terms(
    config: TranslationConfig,
    target_language: str,
) -> tuple[dict[str, str], dict[str, str]]:
    """Extract language-specific UI terms and glossary from config."""
    ui_terms = {
        term: translations[target_language]
        for term, translations in config.ui_terms.items()
        if target_language in translations
    }
    glossary = {
        term: translations[target_language]
        for term, translations in config.glossary.items()
        if target_language in translations
    }
    return ui_terms, glossary


def _format_terms_block(
    ui_terms: dict[str, str],
    glossary: dict[str, str],
) -> str:
    """Format UI terms and glossary as a text block for prompts."""
    lines: list[str] = []

    if ui_terms:
        lines.append("")
        lines.append("UI TERMS (must match the software interface exactly):")
        for term, translation in ui_terms.items():
            lines.append(f'  "{term}" -> "{translation}"')

    if glossary:
        lines.append("")
        lines.append("GLOSSARY (use these translations for consistency):")
        for term, translation in glossary.items():
            lines.append(f'  "{term}" -> "{translation}"')

    return "\n".join(lines)


def _format_examples(config: TranslationConfig, target_language: str) -> str:
    """Format few-shot examples for a target language."""
    relevant = [
        ex
        for ex in config.few_shot_examples
        if ex.get("target_language") == target_language
    ]
    if not relevant:
        return ""

    lines = ["", "EXAMPLES:"]
    for ex in relevant[:3]:
        lines.append(f'  English: {ex["source"]}')
        lines.append(f'  {LANGUAGE_NAMES.get(target_language, target_language)}: {ex["translation"]}')
        lines.append("")
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Translation Prompt (system + user split, JSON output)
# -----------------------------------------------------------------------------


def _build_translation_system(
    target_language: str,
    config: TranslationConfig,
) -> str:
    """Build the system message for translation (shared by single and batch)."""
    lang_name = LANGUAGE_NAMES.get(target_language, target_language)
    ui_terms, glossary = _get_language_terms(config, target_language)

    system_parts = [
        f"You are a professional translator. Translate English to {lang_name}.",
        "",
        "RULES:",
        FORMATTING_RULES,
        "- Do NOT add commentary, explanations, or notes — only the translation",
        "",
        "Your translation MUST be roughly the same length as the source text.",
        "If the source is a full paragraph, your translation must be a full paragraph.",
    ]

    if config.notes:
        system_parts.extend(["", "GUIDELINES:", config.notes.strip()])

    system_parts.append(_format_terms_block(ui_terms, glossary))
    system_parts.append(_format_examples(config, target_language))

    return "\n".join(system_parts)


def _parse_glossary_key(key: str) -> tuple[str, str]:
    """Parse a glossary key like 'download (verb)' into ('download', 'verb').

    Returns (term, pos) where pos is empty string if not present.
    """
    if key.endswith(")") and " (" in key:
        term, pos = key.rsplit(" (", 1)
        return term, pos[:-1]
    return key, ""


def _find_glossary_terms(
    source_text: str,
    target_language: str,
    config: TranslationConfig,
) -> list[tuple[str, str, str]]:
    """Find glossary terms present in the source text.

    Returns list of (term, pos, translation) tuples.
    Matches are case-insensitive. Longer terms are preferred over shorter ones
    to avoid matching substrings (e.g. "activity identifier" over "activity"),
    unless different parts of speech have different translations.
    """
    source_lower = source_text.lower()
    matches: list[tuple[str, str, str]] = []

    for key, translations in config.glossary.items():
        translation = translations.get(target_language, "")
        if not translation:
            continue
        term, pos = _parse_glossary_key(key)
        # Word-boundary match (consistent with checks.check_glossary_terms) so
        # we don't inject a glossary instruction for "result" inside "resulting".
        if re.search(r'\b' + re.escape(term.lower()) + r'\b', source_lower):
            matches.append((term, pos, translation))

    # Remove terms that are substrings of longer matched terms,
    # but only if they have the same translation (keep both noun/verb forms)
    to_remove = []
    for i, (short_term, _, short_trans) in enumerate(matches):
        for long_term, _, long_trans in matches:
            if (
                short_term != long_term
                and short_term.lower() in long_term.lower()
                and short_trans == long_trans
            ):
                to_remove.append(i)
                break
    return [m for i, m in enumerate(matches) if i not in to_remove]


def build_translation_prompt(
    source_text: str,
    target_language: str,
    config: TranslationConfig,
) -> tuple[str, str]:
    """Build system and user messages for translating a single string.

    Returns (system_message, user_message) tuple.
    The caller should request JSON output format.
    The expected response is: {"translation": "..."}
    """
    system = _build_translation_system(target_language, config)
    system += '\n\nYou MUST respond with JSON: {"translation": "your translation here"}'

    # Highlight glossary terms found in this specific source text
    matched_terms = _find_glossary_terms(source_text, target_language, config)
    if matched_terms:
        term_lines = []
        for term, pos, translation in matched_terms:
            if pos:
                term_lines.append(f'  "{term}" ({pos}) → "{translation}"')
            else:
                term_lines.append(f'  "{term}" → "{translation}"')
        user_msg = (
            "This text contains the following glossary terms — "
            "you MUST use these exact translations:\n"
            + "\n".join(term_lines)
            + f"\n\n{source_text}"
        )
    else:
        user_msg = source_text

    return system, user_msg


# -----------------------------------------------------------------------------
# Document Review Prompt
# -----------------------------------------------------------------------------


def build_review_prompt(
    entries: list[dict],
    target_language: str,
    config: TranslationConfig,
    file_name: str,
) -> str:
    """Build a review prompt for translations in a document.

    Args:
        entries: List of dicts with keys: index, source, translation, location
        target_language: Language code
        config: Translation config
        file_name: Name of the PO file being reviewed

    Expected JSON response:
        {"status": "approved", "revisions": []}
        or
        {"status": "revised", "revisions": [{"index": 0, "revised": "...", "reason": "..."}]}
    """
    lang_name = LANGUAGE_NAMES.get(target_language, target_language)
    ui_terms, glossary = _get_language_terms(config, target_language)

    prompt_parts = [
        f"You are reviewing the {lang_name} translation of: {file_name}",
        "",
        "Only flag translations that have REAL ERRORS. Do NOT suggest stylistic preferences.",
        "",
        "NOTE: URL language codes and formatting markers (bold, italic, code, links)",
        "are checked separately by automated tools. Do NOT flag those here.",
        "DO flag: added content not in the source, or missing parts of the source meaning.",
        "",
        "REVISE only if you can name ONE of these specific error categories:",
        '- "incomplete" — parts of the source meaning are missing from the translation',
        '- "mistranslation" — the translation says something DIFFERENT from the source',
        '- "glossary" — a glossary/UI term is translated incorrectly (check terms list below)',
        '- "register" — wrong formality (e.g. informal "tu" instead of formal "vous")',
        "",
        "If the problem is not clearly one of these, do NOT revise — approve it.",
        "",
        "EVIDENCE REQUIRED FOR EVERY REVISION:",
        '- "category": exactly one of the four categories above',
        '- "source_span": the exact words quoted from the SOURCE that are mistranslated',
        '  or omitted (copy them verbatim — they must appear in the source text)',
        '- "translation_span": the exact words quoted from the current TRANSLATION that',
        '  are wrong (use "" only for an "incomplete" omission with nothing to quote)',
        "A revision missing a category, or whose source_span is not actually present in",
        "the source text, will be DISCARDED — so do not invent evidence to justify a",
        "stylistic preference.",
        "",
        "Do NOT revise for:",
        "- URL language codes (/en/ vs /fr/ etc.) — handled by automated checks",
        "- Formatting markers (bold, links, code) — handled by automated checks",
        "- Stylistic preferences (both phrasings are correct)",
        "- Punctuation style (em-dash vs hyphen, etc.)",
        "- Minor word order differences that don't change meaning",
        "",
        "CRITICAL — FORMATTING IN REVISIONS:",
        "- Your revised text MUST have EXACTLY the same formatting markers as the source.",
        "- If the source has NO bold (**), your revision must have NO bold.",
        "- If the source has NO inline code (``), your revision must have NO inline code.",
        "- Do NOT add emphasis, bold, or any other formatting that is absent from the source.",
        "- Count the formatting markers in the source and ensure your revision matches.",
        "",
        "RULES FOR ANY REVISIONS YOU MAKE:",
        FORMATTING_RULES,
        _format_terms_block(ui_terms, glossary),
    ]

    if config.notes:
        prompt_parts.extend(["", "GUIDELINES:", config.notes.strip()])

    prompt_parts.extend([
        "",
        "=" * 60,
        "TRANSLATIONS TO REVIEW:",
        "",
    ])

    for entry in entries:
        prompt_parts.extend([
            f"[{entry['index']}] {entry.get('location', '')}",
            f"  SOURCE: {entry['source']}",
            f"  TRANSLATION: {entry['translation']}",
            "",
        ])

    prompt_parts.extend([
        "=" * 60,
        "",
        "Respond with JSON. If all translations are good (most should be):",
        '{"status": "approved", "revisions": []}',
        "",
        "If any have real errors:",
        '{"status": "revised", "revisions": [',
        '  {"index": 0, "category": "mistranslation",',
        '   "source_span": "exact words from the source",',
        '   "translation_span": "exact words from the translation that are wrong",',
        '   "revised": "the COMPLETE corrected translation",',
        '   "reason": "brief explanation"}',
        "]}",
        "",
        "IMPORTANT:",
        "- Only include entries with real errors, not stylistic preferences",
        "- When in doubt, approve — do not change things that are already correct",
        '- Every revision MUST include "category" and a real "source_span"',
        '- The "revised" field must contain the COMPLETE corrected translation',
        "- Return ONLY valid JSON",
    ])

    return "\n".join(prompt_parts)


# -----------------------------------------------------------------------------
# Site-Wide Review Prompt
# -----------------------------------------------------------------------------


def build_site_review_prompt(
    entries: list[dict],
    target_language: str,
    config: TranslationConfig,
) -> str:
    """Build a site-wide review prompt for cross-file consistency.

    Args:
        entries: List of dicts with keys: index, file_name, source, translation, location
        target_language: Language code
        config: Translation config

    Expected JSON response:
        {"status": "approved", "issues": []}
        or
        {"status": "issues_found", "issues": [
            {"description": "...", "occurrences": [
                {"index": 0, "file_name": "...", "current": "...", "revised": "..."}
            ]}
        ]}
    """
    lang_name = LANGUAGE_NAMES.get(target_language, target_language)
    ui_terms, glossary = _get_language_terms(config, target_language)

    prompt_parts = [
        f"You are reviewing ALL {lang_name} translations across an entire documentation site.",
        "",
        "Focus on CROSS-FILE issues:",
        "1. Same concept translated differently across files",
        "2. Glossary violations",
        "3. Tonal inconsistencies (but check English source first — if source tone varies, translation should too)",
        "",
        "CRITICAL — FORMATTING IN REVISIONS:",
        "- Your revised text MUST have EXACTLY the same formatting markers as the source.",
        "- If the source has NO bold (**), your revision must have NO bold.",
        "- Do NOT add emphasis, bold, or any formatting that is absent from the source.",
        "",
        "RULES FOR ANY REVISIONS YOU MAKE:",
        FORMATTING_RULES,
        _format_terms_block(ui_terms, glossary),
    ]

    if config.notes:
        prompt_parts.extend(["", "GUIDELINES:", config.notes.strip()])

    prompt_parts.extend([
        "",
        "=" * 60,
        "ALL TRANSLATIONS:",
        "",
    ])

    for entry in entries:
        prompt_parts.extend([
            f"[{entry['index']}] {entry['file_name']} | {entry.get('location', '')}",
            f"  SOURCE: {entry['source']}",
            f"  TRANSLATION: {entry['translation']}",
            "",
        ])

    prompt_parts.extend([
        "=" * 60,
        "",
        "Respond with JSON. If consistent:",
        '{"status": "approved", "issues": []}',
        "",
        "If issues found:",
        '{"status": "issues_found", "issues": [',
        '  {"description": "Same term translated differently",',
        '   "occurrences": [',
        '     {"index": 5, "file_name": "a.po", "current": "...", "revised": "..."},',
        '     {"index": 12, "file_name": "b.po", "current": "...", "revised": "..."}',
        "   ]}",
        "]}",
        "",
        "IMPORTANT:",
        "- Focus on CROSS-FILE issues only",
        "- Group related occurrences together",
        "- Provide complete revised translation for each occurrence",
        "- Return ONLY valid JSON",
    ])

    return "\n".join(prompt_parts)


# -----------------------------------------------------------------------------
# Fuzzy Validation Prompt
# -----------------------------------------------------------------------------


def build_correction_prompt(
    source_text: str,
    current_translation: str,
    issues: list[dict],
    target_language: str,
    config: TranslationConfig,
) -> tuple[str, str]:
    """Build system and user messages for correcting a translation.

    Args:
        source_text: Original English source
        current_translation: The current (flawed) translation
        issues: List of issue dicts from deterministic checks
        target_language: Language code
        config: Translation config

    Returns (system_message, user_message) tuple.
    The caller should request JSON output format.
    The expected response is: {"translation": "..."}
    """
    system = _build_translation_system(target_language, config)
    system += (
        "\n\nYou are correcting a translation that has specific problems. "
        "Fix ONLY the listed problems. Keep everything else exactly the same."
        '\n\nYou MUST respond with JSON: {"translation": "your corrected translation here"}'
    )

    issue_lines = "\n".join(f"- {issue['description']}" for issue in issues)
    user_msg = (
        f"SOURCE TEXT:\n{source_text}\n\n"
        f"CURRENT TRANSLATION:\n{current_translation}\n\n"
        f"PROBLEMS TO FIX:\n{issue_lines}\n\n"
        "Return the COMPLETE corrected translation. "
        "Do NOT summarize or explain — return only the full corrected text."
    )

    return system, user_msg


def build_fuzzy_validation_prompt(
    entries: list[dict],
    target_language: str,
    config: TranslationConfig,
) -> str:
    """Build a prompt for validating fuzzy translations.

    Args:
        entries: List of dicts with keys: index, file_name, source, translation, location
        target_language: Language code
        config: Translation config

    Expected JSON response:
        {"validations": [
            {"index": 0, "decision": "approve|revise", "revised": "...|null", "reason": "..."}
        ]}
    """
    lang_name = LANGUAGE_NAMES.get(target_language, target_language)
    ui_terms, glossary = _get_language_terms(config, target_language)

    prompt_parts = [
        f"You are reviewing {lang_name} translations marked as 'fuzzy' (needing review).",
        "",
        "These were flagged because the source text CHANGED. The current translation",
        "was carried over from an OLD, near-matching source by translation memory, so",
        "it may describe the previous wording — or even a different item entirely.",
        "Treat each translation as suspect until you have checked it against the",
        "CURRENT source shown below.",
        "",
        "For each entry, decide: approve (translation already conveys the COMPLETE",
        "current source meaning) or revise (anything is missing, wrong, or stale).",
        "",
        "CRITERIA — revise if ANY of these fail:",
        "1. The translation conveys the COMPLETE current source meaning — no clause",
        "   from the source is missing, and nothing extra has been added.",
        "2. Cross-reference anchors match EXACTLY. If the source ends with",
        "   '<faq_3>', the translation must end with '<faq_3>', not '<faq_1>'.",
        "3. A leading item number matches EXACTLY (source '\\6.' -> translation '\\6.').",
        "4. Formatting is preserved.",
        "Stale anchors or numbers are a sure sign of carry-over from a renumbered",
        "list — revise the whole entry to match the current source, do not approve it.",
        "",
        "RULES FOR ANY REVISIONS YOU MAKE:",
        FORMATTING_RULES,
        _format_terms_block(ui_terms, glossary),
    ]

    if config.notes:
        prompt_parts.extend(["", "GUIDELINES:", config.notes.strip()])

    prompt_parts.extend([
        "",
        "=" * 60,
        "FUZZY TRANSLATIONS:",
        "",
    ])

    for entry in entries:
        prompt_parts.extend([
            f"[{entry['index']}] {entry['file_name']} | {entry.get('location', '')}",
            f"  SOURCE: {entry['source']}",
            f"  TRANSLATION: {entry['translation']}",
            "",
        ])

    prompt_parts.extend([
        "=" * 60,
        "",
        "Respond with JSON:",
        '{"validations": [',
        '  {"index": 0, "decision": "approve", "revised": null, "reason": "accurate translation"},',
        '  {"index": 1, "decision": "revise", "revised": "corrected text", "reason": "missing clause"}',
        "]}",
        "",
        "IMPORTANT:",
        "- Minor stylistic differences are OK — do not churn pure wording preferences",
        "- But DO revise for any missing meaning, mismatched anchor, or stale number",
        "- A 'revise' MUST supply the COMPLETE corrected translation in 'revised'",
        "- Return ONLY valid JSON",
    ])

    return "\n".join(prompt_parts)
