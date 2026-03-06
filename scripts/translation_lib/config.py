"""
Configuration constants and TranslationConfig class.
"""

import csv
import json
import time
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")


# -----------------------------------------------------------------------------
# Path Configuration
# -----------------------------------------------------------------------------

# Shared assets live in this tool's repo
TOOL_DIR = Path(__file__).parent.parent
TOOL_ROOT = TOOL_DIR.parent
DEFAULT_CONFIG_PATH = TOOL_ROOT / "translation_config.json"
GLOSSARY_CSV_PATH = TOOL_ROOT / "glossary.csv"
GLOSSARY_SOURCE_URL = "https://docs.google.com/spreadsheets/d/1lF5RFp6aL4nksWKrDTtQd4HYVftRyOTWW1KQcKwdRBw"

# Target project paths — set by configure_project()
PROJECT_ROOT: Path | None = None
DOCS_DIR: Path | None = None
LOCALE_DIR: Path | None = None
POT_DIR: Path | None = None
UI_TERMS_XLSX_PATH: Path | None = None

LOCAL_CONFIG_FILENAME = "translation_config.json"


def configure_project(project_path: str | Path) -> None:
    """Set target project paths. Must be called before any translation operations."""
    global PROJECT_ROOT, DOCS_DIR, LOCALE_DIR, POT_DIR, UI_TERMS_XLSX_PATH
    PROJECT_ROOT = Path(project_path).resolve()
    DOCS_DIR = PROJECT_ROOT / "docs"
    LOCALE_DIR = DOCS_DIR / "locale"
    POT_DIR = DOCS_DIR / "_build" / "locale"
    UI_TERMS_XLSX_PATH = PROJECT_ROOT / "scripts" / "ui_terms.xlsx"
    if not DOCS_DIR.exists():
        raise FileNotFoundError(f"docs/ directory not found in {PROJECT_ROOT}")


# -----------------------------------------------------------------------------
# Language Configuration
# -----------------------------------------------------------------------------

# Languages enabled for translation by default
SUPPORTED_LANGUAGES = ["fr", "es"]

LANGUAGE_NAMES = {
    "fr": "French",
    "es": "Spanish",
    "pt": "Portuguese",
}


def detect_languages() -> list[str]:
    """Auto-detect languages that already have locale directories."""
    if LOCALE_DIR is None or not LOCALE_DIR.exists():
        return []
    return sorted(
        d.name for d in LOCALE_DIR.iterdir()
        if d.is_dir() and (d / "LC_MESSAGES").exists()
    )


def scaffold_language(language: str) -> None:
    """Create the locale directory structure for a new language."""
    lang_dir = LOCALE_DIR / language / "LC_MESSAGES"
    lang_dir.mkdir(parents=True, exist_ok=True)
    print(f"  Scaffolded locale directory: {lang_dir}")


# -----------------------------------------------------------------------------
# Model Configuration
# -----------------------------------------------------------------------------

LLM_MODEL = "mistral-large-latest"

# Site-wide review chunking
SITE_REVIEW_CHUNK_SIZE = 50  # Max translations per LLM call


# -----------------------------------------------------------------------------
# API Retry Configuration
# -----------------------------------------------------------------------------

MAX_RETRIES = 3
INITIAL_RETRY_DELAY = 1.0  # seconds
MAX_RETRY_DELAY = 30.0  # seconds

# Rate limit retries are more generous — the user expects to wait
RATE_LIMIT_MAX_RETRIES = 10
RATE_LIMIT_MAX_DELAY = 120.0  # seconds


def _get_rate_limit_delay(exception: Exception) -> float | None:
    """If this is a rate limit error (429), return the recommended wait time.

    Checks the Retry-After header if present, otherwise returns a default.
    Returns None if this is not a rate limit error.
    """
    status_code = getattr(exception, "status_code", None)
    if status_code != 429:
        return None

    headers = getattr(exception, "headers", {})
    retry_after = headers.get("retry-after")
    if retry_after:
        try:
            return float(retry_after)
        except (ValueError, TypeError):
            pass

    return 30.0  # default if no Retry-After header


def with_retry(
    max_retries: int = MAX_RETRIES,
    initial_delay: float = INITIAL_RETRY_DELAY,
    max_delay: float = MAX_RETRY_DELAY,
    retryable_exceptions: tuple = (Exception,),
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Decorator that adds exponential backoff retry logic to a function.

    Rate limit errors (HTTP 429) get special treatment: more retries,
    longer waits, and Retry-After header support.
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last_exception = None
            delay = initial_delay
            attempt = 0
            effective_max = max_retries

            while attempt <= effective_max:
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as e:
                    last_exception = e

                    rate_limit_delay = _get_rate_limit_delay(e)
                    if rate_limit_delay is not None:
                        # Rate limited — be patient
                        effective_max = RATE_LIMIT_MAX_RETRIES
                        wait = min(rate_limit_delay, RATE_LIMIT_MAX_DELAY)
                        if attempt < effective_max:
                            mins, secs = divmod(int(wait), 60)
                            wait_str = f"{mins}m{secs:02d}s" if mins else f"{wait:.0f}s"
                            print(f"    Rate limited. Waiting {wait_str}... (attempt {attempt + 1}/{effective_max})")
                            time.sleep(wait)
                            attempt += 1
                            continue
                    elif attempt < effective_max:
                        print(f"    Retry {attempt + 1}/{effective_max} after {delay:.1f}s: {e}")
                        time.sleep(delay)
                        delay = min(delay * 2, max_delay)

                    attempt += 1

            raise last_exception

        return wrapper
    return decorator


# -----------------------------------------------------------------------------
# Glossary Loading
# -----------------------------------------------------------------------------


def load_ui_terms_from_xlsx(path: Path) -> dict[str, dict[str, str]]:
    """
    Load UI terms from an Excel file with multiple sheets.

    Expected columns: Key, Filename, en, fr, es
    - Key: internal string name (used for reference/notes)
    - Filename: where it appears in the software
    - en: English text (used as the glossary term)
    - fr: French translation
    - es: Spanish translation

    Returns dict mapping English UI text to {fr: ..., es: ..., _note: ...}
    """
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise ImportError(
            "openpyxl is required to load UI terms. Run: pip install openpyxl"
        )

    if not path.exists():
        return {}

    ui_terms: dict[str, dict[str, str]] = {}
    wb = load_workbook(path, read_only=True, data_only=True)

    for sheet_name in wb.sheetnames:
        sheet = wb[sheet_name]
        rows = list(sheet.iter_rows(values_only=True))

        if not rows:
            continue

        # Find header row and column indices
        header_row = rows[0]
        if header_row is None:
            continue

        # Normalize headers to lowercase for flexible matching
        headers = {
            str(h).lower().strip(): i
            for i, h in enumerate(header_row)
            if h is not None
        }

        # Find required columns
        en_col = headers.get("en")
        fr_col = headers.get("fr")
        es_col = headers.get("es")
        key_col = headers.get("key")
        filename_col = headers.get("filename")

        if en_col is None:
            # Skip sheets without English column
            continue

        # Process data rows
        for row in rows[1:]:
            if row is None or len(row) <= en_col:
                continue

            en_text = row[en_col]
            if not en_text or not str(en_text).strip():
                continue

            en_text = str(en_text).strip()
            entry: dict[str, str] = {}

            # Get translations
            if fr_col is not None and len(row) > fr_col and row[fr_col]:
                entry["fr"] = str(row[fr_col]).strip()
            if es_col is not None and len(row) > es_col and row[es_col]:
                entry["es"] = str(row[es_col]).strip()

            # Build note from key and filename
            note_parts = []
            if key_col is not None and len(row) > key_col and row[key_col]:
                note_parts.append(f"Key: {row[key_col]}")
            if filename_col is not None and len(row) > filename_col and row[filename_col]:
                note_parts.append(f"File: {row[filename_col]}")
            note_parts.append(f"Sheet: {sheet_name}")
            if note_parts:
                entry["_note"] = " | ".join(note_parts)

            # Only add if we have at least one translation
            if "fr" in entry or "es" in entry:
                # If term already exists, prefer to keep it (first occurrence wins)
                # unless the new entry has more translations
                if en_text not in ui_terms:
                    ui_terms[en_text] = entry
                else:
                    # Merge: add missing translations
                    existing = ui_terms[en_text]
                    if "fr" not in existing and "fr" in entry:
                        existing["fr"] = entry["fr"]
                    if "es" not in existing and "es" in entry:
                        existing["es"] = entry["es"]

    wb.close()
    return ui_terms


def load_glossary_from_csv(path: Path) -> dict[str, dict[str, str]]:
    """
    Load glossary from CSV file.

    Expected columns: term, pos, Agreed, definition, source, comments, ES, FR
    Returns dict mapping English terms to {fr: ..., es: ..., _note: ...}
    """
    glossary = {}

    with open(path, encoding="utf-8", newline="") as f:
        # Detect delimiter (tab or comma)
        first_line = f.readline()
        f.seek(0)
        delimiter = "\t" if "\t" in first_line else ","

        reader = csv.DictReader(f, delimiter=delimiter)

        for row in reader:
            # Normalize keys to lowercase
            row = {k.lower().strip(): v.strip() for k, v in row.items() if k}

            term = row.get("term", "")
            if not term:
                continue

            entry = {}
            pos = row.get("pos", "").strip()

            # Get translations
            if row.get("fr"):
                entry["fr"] = row["fr"]
            if row.get("es"):
                entry["es"] = row["es"]

            # Build note from definition and comments
            note_parts = []
            if row.get("definition"):
                note_parts.append(row["definition"])
            if row.get("comments"):
                note_parts.append(f"Note: {row['comments']}")
            if note_parts:
                entry["_note"] = " ".join(note_parts)

            if entry:  # Only add if we have some data
                # If term already exists with different translations,
                # disambiguate with part of speech (e.g. "result (noun)")
                if term in glossary and pos:
                    key = f"{term} ({pos.lower()})"
                else:
                    key = term
                glossary[key] = entry

    return glossary


# -----------------------------------------------------------------------------
# Translation Configuration
# -----------------------------------------------------------------------------

@dataclass
class TranslationConfig:
    """Configuration for translation prompts."""
    glossary: dict[str, dict[str, str]] = field(default_factory=dict)
    ui_terms: dict[str, dict[str, str]] = field(default_factory=dict)
    few_shot_examples: list[dict] = field(default_factory=list)
    notes: str = ""

    @classmethod
    def load(cls, config_path: Path | None = None) -> "TranslationConfig":
        """Load config by merging global (this tool) + local (target project) configs.

        Merge strategy:
        - few_shot_examples: concatenated (local examples added after global)
        - notes: concatenated with separator (global notes + local notes)
        - glossary: loaded from shared glossary.csv
        - ui_terms: loaded from target project's ui_terms.xlsx
        """
        global_path = config_path or DEFAULT_CONFIG_PATH

        # Load global config
        global_data = {}
        if global_path.exists():
            with open(global_path, "r", encoding="utf-8") as f:
                global_data = json.load(f)

        # Load local config from target project (if exists)
        local_data = {}
        if PROJECT_ROOT is not None:
            local_path = PROJECT_ROOT / "scripts" / LOCAL_CONFIG_FILENAME
            if local_path.exists():
                with open(local_path, "r", encoding="utf-8") as f:
                    local_data = json.load(f)

        # Merge: few_shot_examples concatenated, notes concatenated
        examples = global_data.get("few_shot_examples", []) + local_data.get("few_shot_examples", [])

        global_notes = global_data.get("notes", "")
        local_notes = local_data.get("notes", "")
        if global_notes and local_notes:
            notes = global_notes + "\n\nPROJECT-SPECIFIC NOTES:\n" + local_notes
        else:
            notes = global_notes or local_notes

        # Load glossary and UI terms
        glossary = load_glossary_from_csv(GLOSSARY_CSV_PATH) if GLOSSARY_CSV_PATH.exists() else {}
        ui_terms = load_ui_terms_from_xlsx(UI_TERMS_XLSX_PATH) if UI_TERMS_XLSX_PATH else {}

        return cls(
            glossary=glossary,
            ui_terms=ui_terms,
            few_shot_examples=examples,
            notes=notes,
        )
