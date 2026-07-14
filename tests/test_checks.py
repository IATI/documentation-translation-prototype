"""Tests for deterministic checks.

Coverage is focused on the regressions that show up in the commit history:
- "preserve numbered lists"            -> list-prefix detection / fixing
- "detect italics and improve fidelity"-> italic-span counting and formatting
plus the URL/glossary/length checks and validate_revision, which gate whether
good translations get clobbered or bad ones get written.
"""

import polib
import pytest

from translation_lib.checks import (
    _count_italic_spans,
    _extract_list_prefix,
    auto_fix_entry,
    check_bracket_balance,
    check_formatting_preserved,
    check_glossary_terms,
    check_length_ratio,
    check_list_prefix,
    check_no_llm_artifacts,
    check_numbered_prefix,
    check_ref_targets,
    check_url_integrity,
    check_url_language_codes,
    fix_list_prefix,
    fix_url_integrity,
    fix_url_language_codes,
    is_truncation_suspect,
    run_all_checks,
    validate_revision,
)
from translation_lib.config import TranslationConfig


# -----------------------------------------------------------------------------
# Italic detection — regression for "detect italics and improve content fidelity"
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("plain text", 0),
    ("*one*", 1),
    ("*one* and *two*", 2),
    ("**bold** only", 0),                 # bold must not be counted as italic
    ("**bold** and *italic*", 1),         # bold ignored, italic counted
    ("a ** b ** c", 0),                   # stray bold markers, no italic span
])
def test_count_italic_spans(text, expected):
    assert _count_italic_spans(text) == expected


def test_formatting_flags_dropped_italic():
    issues = check_formatting_preserved("an *important* note", "une note")
    assert any(i["type"] == "formatting" for i in issues)


def test_formatting_flags_added_bold():
    # Source has no bold; translation invents it — must be flagged.
    issues = check_formatting_preserved("plain source", "**gras** source")
    assert any("bold" in i["description"] for i in issues)


def test_formatting_preserved_when_markers_match():
    assert check_formatting_preserved("**bold** and `code`", "**gras** et `code`") == []


def test_formatting_flags_dropped_rst_link():
    src = "See `the docs <https://x.org>`_ now"
    issues = check_formatting_preserved(src, "Voir la doc maintenant")
    assert any("RST link" in i["description"] for i in issues)


# -----------------------------------------------------------------------------
# List prefixes — regression for "preserve numbered lists"
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("1) First", "1) "),
    ("2. Second", "2. "),
    ("(a) Item", "(a) "),
    ("(3) Item", "(3) "),
    ("a) Item", "a) "),
    ("Plain heading", None),
])
def test_extract_list_prefix(text, expected):
    assert _extract_list_prefix(text) == expected


def test_check_list_prefix_flags_dropped_prefix():
    issues = check_list_prefix("1) Do the thing", "Faire la chose")
    assert len(issues) == 1
    assert issues[0]["type"] == "list_prefix"


def test_check_list_prefix_ok_when_preserved():
    assert check_list_prefix("1) Do the thing", "1) Faire la chose") == []


def test_fix_list_prefix_prepends_missing_prefix():
    assert fix_list_prefix("1) Do the thing", "Faire la chose") == "1) Faire la chose"


def test_fix_list_prefix_noop_when_present():
    assert fix_list_prefix("1) Do the thing", "1) Faire la chose") is None


def test_auto_fix_entry_restores_list_prefix():
    entry = polib.POEntry(msgid="2. Step two", msgstr="Étape deux")
    fixes = auto_fix_entry(entry, "fr")
    assert entry.msgstr == "2. Étape deux"
    assert any("list prefix" in f for f in fixes)


# -----------------------------------------------------------------------------
# URL language codes
# -----------------------------------------------------------------------------


def test_fix_url_language_code_on_iati_domain():
    out = fix_url_language_codes("https://iatistandard.org/en/guidance", "fr")
    assert out == "https://iatistandard.org/fr/guidance"


def test_fix_url_language_code_leaves_other_domains_alone():
    url = "https://example.com/en/guidance"
    assert fix_url_language_codes(url, "fr") == url


def test_check_url_language_code_flags_untranslated_path():
    src = "See https://iatistandard.org/en/guidance"
    trans = "Voir https://iatistandard.org/en/guidance"
    issues = check_url_language_codes(src, trans, "fr")
    assert any(i["type"] == "url_language_code" for i in issues)


def test_auto_fix_entry_fixes_url_code():
    entry = polib.POEntry(
        msgid="https://iatistandard.org/en/x",
        msgstr="https://iatistandard.org/en/x",
    )
    auto_fix_entry(entry, "es")
    assert entry.msgstr == "https://iatistandard.org/es/x"


# -----------------------------------------------------------------------------
# Glossary term preservation
# -----------------------------------------------------------------------------


@pytest.fixture
def glossary_config():
    return TranslationConfig(glossary={
        "activity": {"es": "actividad", "fr": "activité"},
        "result": {"es": "resultado", "fr": "résultat"},
    })


def test_glossary_term_present(glossary_config):
    assert check_glossary_terms("Create an activity", "Crear una actividad", "es", glossary_config) == []


def test_glossary_term_missing(glossary_config):
    issues = check_glossary_terms("Create an activity", "Crear una cosa", "es", glossary_config)
    assert any(i["type"] == "glossary_term" for i in issues)


def test_glossary_term_missing_includes_expected_translation(glossary_config):
    # The correction prompt needs the raw term/expected pair, not just the
    # rendered description string, to build a targeted retry.
    issues = check_glossary_terms("Create an activity", "Crear una cosa", "es", glossary_config)
    issue = next(i for i in issues if i["type"] == "glossary_term")
    assert issue["name"] == "activity"
    assert issue["expected"] == "actividad"


def test_glossary_word_boundary_no_false_positive(glossary_config):
    # "result" appears only inside "resulting" — should NOT be required.
    assert check_glossary_terms("the resulting data", "les données obtenues", "fr", glossary_config) == []


def test_glossary_term_recognises_spelling_change_conjugation():
    # simplemma's Spanish lemmatiser doesn't map "descargue" (subjunctive/
    # imperative, g -> gu before e) back to the infinitive "descargar" —
    # a real gap that let an already-correct translation get flagged.
    config = TranslationConfig(glossary={"download": {"es": "descargar"}})
    assert check_glossary_terms(
        "then download the HTML file",
        "luego descargue el archivo HTML",
        "es",
        config,
    ) == []


def test_glossary_term_recognises_gerund_lemmatisation_miss():
    # simplemma also misses "resultando" (gerund) -> "resultar" (infinitive).
    config = TranslationConfig(glossary={"result": {"es": "resultar"}})
    assert check_glossary_terms(
        "this may result in errors",
        "esto puede ir resultando en errores",
        "es",
        config,
    ) == []


def test_glossary_stem_fallback_does_not_match_unrelated_word():
    # Guard against the prefix fallback rubber-stamping an unrelated word
    # that happens to share a short prefix.
    config = TranslationConfig(glossary={"download": {"es": "descargar"}})
    issues = check_glossary_terms(
        "then download the HTML file", "luego coja el archivo HTML", "es", config
    )
    assert any(i["type"] == "glossary_term" for i in issues)


# -----------------------------------------------------------------------------
# Length ratio
# -----------------------------------------------------------------------------


def test_length_ratio_flags_truncation():
    source = "This is a reasonably long English sentence that should translate to similar length."
    issues = check_length_ratio(source, "Court.")
    assert any(i["type"] == "length_ratio" for i in issues)


def test_length_ratio_ignores_short_strings():
    assert check_length_ratio("OK", "X") == []


# -----------------------------------------------------------------------------
# validate_revision — must reject revisions that introduce NEW problems but
# allow ones that merely fail to fully resolve a pre-existing problem.
# -----------------------------------------------------------------------------


def test_validate_revision_rejects_new_formatting_kind():
    # Original drops the bold; revision still drops the bold AND adds a stray
    # italic span. The italic mismatch is a NEW problem and must be reported.
    source = "This is **bold** text"
    original = "Ceci est un texte"
    revised = "Ceci est *un* texte"
    new_issues = validate_revision(source, original, revised, "fr")
    assert any("italic" in i["description"] for i in new_issues)


def test_validate_revision_allows_still_imperfect_length():
    # Both original and revision are "suspiciously long" — same kind of issue,
    # so the revision introduces nothing new and must be allowed.
    source = "Short English source string here."
    original = "x" * (len(source) * 3)
    revised = "y" * (len(source) * 3 - 2)
    assert validate_revision(source, original, revised, "fr") == []


def test_validate_revision_clean_revision_ok():
    source = "This is **bold** text"
    original = "Ceci est un texte"          # drops the bold
    revised = "Ceci est un texte **gras**"  # restores a bold pair
    assert validate_revision(source, original, revised, "fr") == []


# -----------------------------------------------------------------------------
# LLM/JSON artifact leakage — raw model output written as the translation.
# These otherwise pass every other check and get stamped REVIEWED.
# -----------------------------------------------------------------------------


def test_flags_leaked_json_envelope():
    # The model double-wrapped its answer; the nested envelope was written verbatim.
    trans = '\\8. {"translation": "8. ¿Dónde puedo acceder?"}'
    issues = check_no_llm_artifacts("8. Where can I access this?", trans)
    assert any(i["type"] == "llm_artifact" for i in issues)


def test_flags_ref_role_json_envelope():
    trans = '{"translation": ":ref:`¿Por qué hay ceros? <faq>`"}'
    issues = check_no_llm_artifacts(":ref:`Why are there zeros? <faq>`", trans)
    assert any(i["type"] == "llm_artifact" for i in issues)


def test_flags_literal_escape_junk():
    issues = check_no_llm_artifacts("Example questions", "Exemples de questions\\n0:")
    assert any("escape" in i["description"] for i in issues)


def test_literal_escape_ignored_when_in_source():
    # Docs that legitimately discuss a "\n" escape must not be flagged.
    src = "The newline character is written \\n in the file"
    trans = "Le caractère de nouvelle ligne s'écrit \\n dans le fichier"
    assert check_no_llm_artifacts(src, trans) == []


def test_clean_translation_has_no_artifacts():
    assert check_no_llm_artifacts("Where can I access this?", "¿Dónde puedo acceder?") == []


# -----------------------------------------------------------------------------
# URL integrity — text altered inside a link (e.g. the IATI -> IITA acronym rule
# wrongly applied to a URL path, which 404s).
# -----------------------------------------------------------------------------


def test_url_integrity_flags_acronym_substitution_in_path():
    src = "See https://iatistandard.org/en/iati-standard/activity-standard/"
    trans = "Voir https://iatistandard.org/fr/IITA-standard/activity-standard/"
    issues = check_url_integrity(src, trans, "fr")
    assert any(i["type"] == "url_corruption" for i in issues)


def test_url_integrity_accepts_language_adjusted_url():
    src = "See https://iatistandard.org/en/guidance/"
    trans = "Voir https://iatistandard.org/fr/guidance/"
    assert check_url_integrity(src, trans, "fr") == []


def test_url_integrity_ignores_non_iati_domains():
    src = "See https://example.org/iati/thing"
    trans = "Voir https://example.org/iita/chose"
    assert check_url_integrity(src, trans, "fr") == []


def test_fix_url_integrity_restores_corrupted_path():
    src = "See https://iatistandard.org/en/iati-standard/activity-standard/"
    trans = "Voir https://iatistandard.org/fr/IITA-standard/activity-standard/"
    fixed = fix_url_integrity(src, trans, "fr")
    assert fixed == "Voir https://iatistandard.org/fr/iati-standard/activity-standard/"


def test_fix_url_integrity_noop_when_urls_match():
    src = "See https://iatistandard.org/en/guidance/"
    trans = "Voir https://iatistandard.org/fr/guidance/"
    assert fix_url_integrity(src, trans, "fr") is None


def test_fix_url_integrity_bails_when_counts_differ():
    # Ambiguous: can't positionally match, so leave it for LLM/manual review.
    src = "See https://iatistandard.org/en/a and https://iatistandard.org/en/b"
    trans = "Voir https://iatistandard.org/fr/IITA-a"
    assert fix_url_integrity(src, trans, "fr") is None


def test_auto_fix_entry_restores_corrupted_url():
    entry = polib.POEntry(
        msgid="https://iatistandard.org/en/iati-standard/x",
        msgstr="https://iatistandard.org/fr/IITA-standard/x",
    )
    fixes = auto_fix_entry(entry, "fr")
    assert entry.msgstr == "https://iatistandard.org/fr/iati-standard/x"
    assert any("URL" in f for f in fixes)


# -----------------------------------------------------------------------------
# Bracket balance — a leaked fragment (e.g. a reviewer's note) unbalances the
# translation's parentheses relative to a balanced source.
# -----------------------------------------------------------------------------


def test_bracket_balance_flags_leaked_note():
    src = "See individual IATI activities in d-portal"
    trans = "Ver actividades individuales de IATI en d-portal, señalar para consistencia)"
    issues = check_bracket_balance(src, trans)
    assert any(i["type"] == "bracket_balance" for i in issues)


def test_bracket_balance_ok_when_both_balanced():
    src = "Data (see note) here"
    trans = "Datos (ver nota) aquí"
    assert check_bracket_balance(src, trans) == []


def test_bracket_balance_ignores_source_that_is_itself_unbalanced():
    # Smiley/emoticon or intentional single paren in source — not our problem.
    src = "Click Save :)"
    trans = "Haga clic en Guardar :)"
    assert check_bracket_balance(src, trans) == []


# -----------------------------------------------------------------------------
# Truncation suspect filter — the recall-oriented gate for the LLM completeness
# check. Deliberately loose: it only decides who is worth an LLM look.
# -----------------------------------------------------------------------------


def test_truncation_suspect_flags_short_translation():
    # The real regression: a full English sentence rendered as a fragment,
    # still within the length_ratio bounds (0.4-2.5) so nothing else catches it.
    src = "Reporting organisations may follow different update schedules."
    trans = "Las organizaciones que presentan el informe"
    assert is_truncation_suspect(src, trans) is True


def test_truncation_suspect_ignores_normal_expansion():
    # FR/ES normally expand English; a longer translation is not a suspect.
    src = "Reporting organisations may follow different update schedules."
    trans = "Las organizaciones informantes pueden seguir distintos calendarios de actualización."
    assert is_truncation_suspect(src, trans) is False


def test_truncation_suspect_ignores_short_sources():
    # Headings / UI labels legitimately stay short and often shrink.
    assert is_truncation_suspect("Searching d-portal", "D-Portal") is False


def test_truncation_suspect_flags_dropped_sentence():
    src = "First we validate the file. Then we publish it to the registry."
    trans = "Primero validamos el archivo y lo publicamos en el registro correctamente hoy."
    # Same length ballpark but one fewer sentence-ending mark -> suspect.
    assert is_truncation_suspect(src, trans) is True


# -----------------------------------------------------------------------------
# Cross-reference anchors and item numbers — regression for the FAQ-renumbering
# carry-over (a fuzzy entry whose <faq_N> anchor / \N. number went stale).
# -----------------------------------------------------------------------------


def test_check_ref_targets_flags_stale_anchor():
    # The classic carry-over: faq_3's question got the faq_1 answer + anchor.
    src = ":ref:`Why can't I see my published data? <faq_3>`"
    trans = ":ref:`¿Qué datos se incluyen? <faq_1>`"
    issues = check_ref_targets(src, trans)
    assert len(issues) == 1
    assert issues[0]["type"] == "ref_target"


def test_check_ref_targets_ok_when_anchor_matches():
    src = ":ref:`What data is included? <faq_1>`"
    trans = ":ref:`¿Qué datos se incluyen? <faq_1>`"
    assert check_ref_targets(src, trans) == []


def test_check_ref_targets_ignores_external_rst_links():
    # `text <url>`_ external links have no :role: prefix and are checked
    # elsewhere — they must not be treated as cross-reference anchors.
    src = "See `the docs <https://x.org/en/>`_"
    trans = "Voir `la doc <https://x.org/fr/>`_"
    assert check_ref_targets(src, trans) == []


def test_check_numbered_prefix_flags_stale_number():
    issues = check_numbered_prefix(
        "\\6. Why are there zeros and dashes?",
        "\\5. ¿Por qué hay ceros y guiones?",
    )
    assert len(issues) == 1
    assert issues[0]["type"] == "numbered_prefix"


def test_check_numbered_prefix_ok_when_number_matches():
    assert check_numbered_prefix(
        "\\6. Why are there zeros and dashes?",
        "\\6. ¿Por qué hay ceros y guiones?",
    ) == []


def test_check_numbered_prefix_noop_without_escaped_number():
    assert check_numbered_prefix("Plain heading", "Titre simple") == []


# -----------------------------------------------------------------------------
# run_all_checks aggregation
# -----------------------------------------------------------------------------


def test_run_all_checks_flags_stale_anchor():
    entry = polib.POEntry(
        msgid=":ref:`Why can't I see my published data? <faq_3>`",
        msgstr=":ref:`¿Qué datos se incluyen? <faq_1>`",
    )
    assert any(i["type"] == "ref_target" for i in run_all_checks(entry, "es"))


def test_run_all_checks_passes_clean_entry():
    entry = polib.POEntry(msgid="Hello world", msgstr="Bonjour le monde")
    assert run_all_checks(entry, "fr") == []


def test_run_all_checks_empty_entry():
    entry = polib.POEntry(msgid="", msgstr="")
    assert run_all_checks(entry, "fr") == []
