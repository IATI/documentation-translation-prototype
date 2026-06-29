# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Intent

This is a one-shot tool: the user points it at an IATI docs repo and it brings that repo's translations up to the consistent standard defined by this codebase, in a single run. The user shouldn't need to intervene, understand translation details, or speak the target languages — they just need to see that progress is being made and that the tool completed successfully.

The user is likely familiar with the source English text and interested in understanding progress, but is not an expert translator. This is an experimental prototype, so clear progress output is important for confidence. When something goes wrong, the user will typically report it back to the tool's developer rather than debug it themselves.

The target repo may have pre-existing translations from various sources (human translators, previous MT pipelines, Google Translate copy/paste). The tool's job is to bring *all* translations — new and pre-existing — to the consistent standard, not just translate untranslated strings.

**When working on this codebase, keep in mind:**
- The tool should remain opinionated and autonomous — it defines what "correct" looks like and gets there without user decisions
- Progress and status output matters: the user should be able to see what's happening at a glance
- Errors should be reported clearly enough for the user to relay them, not necessarily to fix them

## Technical Overview

Uses deterministic logic combined with Mistral AI LLM calls to translate Sphinx-based docs (PO/POT files) into French and Spanish to a consistent standard.

## Setup & Commands

```bash
# Setup
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Recompile dependencies after editing requirements.in
pip-compile requirements.in

# Run the test suite (no API key needed — deterministic checks, PO utils, JSON parsing)
pip install -r requirements-dev.txt
pytest

# All scripts require MISTRAL_API_KEY env var (except --dry-run)
# All scripts take a target docs repo path as first argument

# Translate a docs repo (extract → update PO → translate → review → fuzzy → site-wide review → final validation)
MISTRAL_API_KEY=xyz ./scripts/translate.py /path/to/docs-repo
MISTRAL_API_KEY=xyz ./scripts/translate.py /path/to/docs-repo --language fr
./scripts/translate.py /path/to/docs-repo --dry-run    # preview without API calls
./scripts/translate.py /path/to/docs-repo --verbose    # show every translation (default: compact progress)

# Skip specific pipeline steps
./scripts/translate.py /path/to/docs-repo --skip-extract --skip-review

# Real runs mirror all output to translation-<timestamp>.log in the current directory
# (override with --log PATH). Friendly messages are shown for missing/invalid API keys
# and connectivity problems; full tracebacks go to the log, not the terminal.

# Show translation status/stats (no API key needed)
./scripts/stats.py /path/to/docs-repo
./scripts/stats.py /path/to/docs-repo --language fr --verbose

# Check English source text for errors before translating
MISTRAL_API_KEY=xyz ./scripts/check_english.py /path/to/docs-repo
./scripts/check_english.py /path/to/docs-repo --dry-run  # formatting checks only, no API key needed

# Review existing translations (diagnostic only, does not modify files)
MISTRAL_API_KEY=xyz ./scripts/review.py /path/to/docs-repo
MISTRAL_API_KEY=xyz ./scripts/review.py /path/to/docs-repo --site-wide
```

## Script Roles

- **translate.py** — The main tool. Does the work: extracts, translates, reviews, and fixes. Modifies files. This is what the user runs.
- **check_english.py** — Pre-translation diagnostic. Checks English source strings for spelling, grammar, and broken formatting. Read-only. Run before translating to catch source errors that would propagate into translations.
- **stats.py** — Read-only status dashboard. Shows translation coverage per language/file. No API key needed.
- **review.py** — Diagnostic tool for the developer. Calls the LLM reviewer and reports issues but does not modify files. Use translate.py to update translations.

## Architecture

### Translation Pipeline (translate.py)

Pipeline: `sphinx-build -b gettext` → `sphinx-intl update` → LLM translation → per-file review (up to 2 convergence passes) → fuzzy entry validation → site-wide cross-file consistency review → final deterministic validation pass.

Each translated entry goes through a quality assurance cascade:
1. Auto-fix deterministic issues (e.g., URL language codes)
2. Run deterministic checks (formatting, length ratio, glossary terms)
3. If issues remain: call LLM with targeted correction prompt, validate revision doesn't regress, retry up to 2 times
4. Flag as unfixable if still failing

### Library Structure (scripts/translation_lib/)

- **config.py** — Project path setup (`configure_project()`), `TranslationConfig` dataclass that merges global + local project configs, glossary loading from CSV/XLSX
- **llm_utils.py** — Mistral API wrapper (`call_llm`) with throttling (~3 req/s, half the 6 req/s limit) and `parse_json_response()` which handles markdown blocks, prose wrapping, and common JSON errors. Single model, always `temperature=0`
- **prompts.py** — All LLM prompt builders (translation, review, site-wide review, fuzzy validation, correction). Shared `FORMATTING_RULES` constant
- **checks.py** — Deterministic validators: URL language codes on IATI domains, glossary term preservation, length ratio (0.4x–2.5x), formatting markers (bold, code, RST links)
- **quality.py** — `ensure_entry_quality()` orchestrates the auto-fix → check → LLM correction loop per entry
- **review.py** (lib) — Per-file and site-wide review logic with convergence
- **po_utils.py** — Thin polib wrapper for loading PO files and `is_locked()` check
- **formatting.py** — CLI output helpers (truncate, diff display)

### Key Config Files

- **translation_config.json** — Global guidelines, few-shot examples per language, formatting rules, language-specific notes (formality, dates)
- **glossary.csv** — 61 curated IATI term translations (columns: term, pos, definition, ES, FR). Used in prompts and deterministic checks

### Design Patterns

- **Config merging**: Global config + project-local `scripts/translation_config.json` merged (examples/notes concatenated)
- **Retry decorator** (`with_retry`): Exponential backoff for general errors (max 3), extended retries for HTTP 429 rate limits (max 10, up to 120s, respects Retry-After)
- **JSON robustness**: `parse_json_response()` tries direct parse → strip markdown blocks → extract outermost `{...}` → fix trailing commas/control chars
- **LLM responses**: Always JSON with specific schemas (e.g., `{"translation": "..."}`, `{"status": "approved|revised", "revisions": [...]}`)

## Locked Entries

Individual PO entries can be marked as "locked" to prevent the pipeline from modifying them. This is useful when a translation has been manually adjusted (e.g., per user feedback) and should not be overwritten.

**How to lock an entry:** Add a translator comment starting with `LOCKED` to the entry in the .po file:

```
# LOCKED
#: path/to/source.rst:42
msgid "Original text"
msgstr "Manually adjusted translation"
```

Optionally include a reason: `# LOCKED: adjusted per user feedback`.

**Behaviour:**
- Locked entries are skipped in all pipeline phases: translation, review, quality checks, and fuzzy handling
- If a locked entry becomes fuzzy (source text changed), the pipeline warns the user but does not auto-resolve — manual review is required
- `stats.py` reports locked entry counts alongside translated/fuzzy/missing
- The `LOCKED` marker uses PO translator comments (`# ` prefix / `entry.tcomment`), which persist through `sphinx-intl update`

## Key Details

- Python 3.13, modern type hints (dataclasses, `tuple[...]`, `dict[...]`)
- Supported languages: `["fr", "es"]` (Portuguese `pt` extensible)
- Single LLM model (`mistral-large-latest`) for all calls, always `temperature=0` for consistency
- Translations use formal address: "vous" (FR), "usted" (ES)
- `polib.POEntry` is the canonical entry representation throughout
