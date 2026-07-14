#!/usr/bin/env python3
"""
Review existing translations for quality.

Diagnostic tool — shows potential issues; does not modify files.
Use translate.py to update translations.

Usage:
    python scripts/review.py ../iati-publisher-docs                          # All languages, site-wide review
    python scripts/review.py ../iati-publisher-docs --language fr            # Single language
    python scripts/review.py ../iati-publisher-docs --per-file               # Per-file review only (quicker, cheaper)

Environment Variables:
    MISTRAL_API_KEY: Your Mistral API key
"""

import argparse
import os
import sys
import traceback
from pathlib import Path

try:
    from mistralai import Mistral
except ImportError:
    print("Error: mistralai package not installed. Run: pip install mistralai")
    sys.exit(1)

from translation_lib import (
    LANGUAGE_NAMES,
    SUPPORTED_LANGUAGES,
    TranslationConfig,
    configure_project,
    describe_api_error,
    get_po_files,
    is_locked,
    load_po_file,
)
from translation_lib import config as tl_config
from translation_lib.checks import run_all_checks
from translation_lib.formatting import truncate
from translation_lib.review import review_po_files, review_site_wide


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Review existing translations for quality",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/review.py ../iati-publisher-docs                     # Review all languages (site-wide)
  python scripts/review.py ../iati-publisher-docs --language fr       # Review French only
  python scripts/review.py ../iati-publisher-docs --per-file          # Per-file review only
        """,
    )
    parser.add_argument(
        "project_path",
        help="Path to the target documentation project",
    )
    parser.add_argument(
        "--language", "-l",
        help=f"Target language (default: all — {', '.join(SUPPORTED_LANGUAGES)})",
    )
    parser.add_argument(
        "--per-file",
        action="store_true",
        help="Per-file review only (default: site-wide cross-file consistency)",
    )
    parser.add_argument(
        "--config", "-c",
        type=Path,
        help="Path to translation configuration file (overrides global config)",
    )

    args = parser.parse_args()

    # Configure target project
    try:
        configure_project(args.project_path)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        return 1

    languages = [args.language] if args.language else SUPPORTED_LANGUAGES

    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        print("Error: MISTRAL_API_KEY environment variable not set")
        return 1

    client = Mistral(api_key=api_key)
    config = TranslationConfig.load(args.config)

    print("Translation Review")
    print("=" * 40)
    print(f"Project: {tl_config.PROJECT_ROOT}")

    # Deterministic checks (no API calls)
    check_issues = 0
    for lang in languages:
        lang_name = LANGUAGE_NAMES.get(lang, lang)
        print(f"\nDeterministic checks for {lang_name}...")
        lang_issues = 0
        for po_path in get_po_files(lang):
            po = load_po_file(po_path)
            for entry in po.translated_entries():
                if is_locked(entry):
                    continue
                issues = run_all_checks(entry, lang, config)
                for issue in issues:
                    print(f"  {po_path.name}: \"{truncate(entry.msgid)}\"")
                    print(f"    {issue['description']}")
                    lang_issues += 1
        if lang_issues == 0:
            print("  All checks passed")
        check_issues += lang_issues

    # LLM review
    llm_issues = 0
    try:
        for lang in languages:
            if args.per_file:
                issues, _ = review_po_files(
                    client, lang, config, apply=False
                )
            else:
                issues, _ = review_site_wide(
                    client, lang, config, apply=False
                )
            llm_issues += issues
    except Exception as e:
        friendly = describe_api_error(e)
        print()
        if friendly:
            print(f"Error: {friendly}")
            return 1
        print(f"Unexpected error: {type(e).__name__}: {e}")
        traceback.print_exc()
        return 1

    print("\n" + "=" * 40)
    total = check_issues + llm_issues
    print(f"Found {check_issues} deterministic issues, {llm_issues} LLM review issues.")
    if total > 0:
        print("Use translate.py to update translations.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
