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
    check_formatting_preserved,
    check_glossary_terms,
    check_length_ratio,
    check_list_prefix,
    check_url_language_codes,
    fix_list_prefix,
    fix_url_language_codes,
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


def test_glossary_word_boundary_no_false_positive(glossary_config):
    # "result" appears only inside "resulting" — should NOT be required.
    assert check_glossary_terms("the resulting data", "les données obtenues", "fr", glossary_config) == []


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
# run_all_checks aggregation
# -----------------------------------------------------------------------------


def test_run_all_checks_passes_clean_entry():
    entry = polib.POEntry(msgid="Hello world", msgstr="Bonjour le monde")
    assert run_all_checks(entry, "fr") == []


def test_run_all_checks_empty_entry():
    entry = polib.POEntry(msgid="", msgstr="")
    assert run_all_checks(entry, "fr") == []
