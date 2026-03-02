#!/usr/bin/env python3
"""
Review existing translations for quality.

Read-only by default — shows issues without modifying files.
Use --apply to write corrections.

Usage:
    python scripts/review.py ../iati-publisher-docs                          # All languages, per-file review
    python scripts/review.py ../iati-publisher-docs --language fr            # Single language
    python scripts/review.py ../iati-publisher-docs --site-wide              # Cross-file consistency
    python scripts/review.py ../iati-publisher-docs --apply                  # Apply corrections
    python scripts/review.py ../iati-publisher-docs --dry-run                # Preview what would be reviewed

Environment Variables:
    MISTRAL_API_KEY: Your Mistral API key (required unless --dry-run)
"""

import argparse
import os
import sys
from pathlib import Path

try:
    from mistralai import Mistral
except ImportError:
    print("Error: mistralai package not installed. Run: pip install mistralai")
    sys.exit(1)

from translation_lib import (
    SUPPORTED_LANGUAGES,
    TranslationConfig,
    configure_project,
)
from translation_lib import config as tl_config
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
  python scripts/review.py ../iati-publisher-docs                     # Review all languages (report only)
  python scripts/review.py ../iati-publisher-docs --language fr       # Review French only
  python scripts/review.py ../iati-publisher-docs --site-wide         # Cross-file consistency check
  python scripts/review.py ../iati-publisher-docs --apply             # Apply suggested corrections
  python scripts/review.py ../iati-publisher-docs --dry-run           # Preview what would be reviewed
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
        "--site-wide",
        action="store_true",
        help="Cross-file consistency review (default: per-file)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply suggested corrections (default: report only)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be reviewed without calling the LLM",
    )
    parser.add_argument(
        "--config", "-c",
        type=Path,
        help="Path to translation configuration file (overrides global config)",
    )

    args = parser.parse_args()

    # Configure target project
    configure_project(args.project_path)

    languages = [args.language] if args.language else SUPPORTED_LANGUAGES

    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key and not args.dry_run:
        print("Error: MISTRAL_API_KEY environment variable not set")
        return 1

    client = Mistral(api_key=api_key) if api_key else None
    config = TranslationConfig.load(args.config)

    mode = "report only" if not args.apply else "apply corrections"
    print(f"Translation Review ({mode})")
    print("=" * 40)
    print(f"Project: {tl_config.PROJECT_ROOT}")

    grand_issues = 0
    grand_applied = 0

    for lang in languages:
        if args.site_wide:
            issues, applied = review_site_wide(
                client, lang, config, apply=args.apply, dry_run=args.dry_run
            )
        else:
            issues, applied = review_po_files(
                client, lang, config, apply=args.apply, dry_run=args.dry_run
            )
        grand_issues += issues
        grand_applied += applied

    print("\n" + "=" * 40)
    if args.dry_run:
        print("Dry run complete.")
    elif args.apply:
        print(f"Found {grand_issues} issues, applied {grand_applied} corrections.")
    else:
        print(f"Found {grand_issues} issues. Use --apply to write corrections.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
