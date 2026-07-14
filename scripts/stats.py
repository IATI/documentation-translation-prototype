#!/usr/bin/env python3
"""
Show translation statistics.

Usage:
    python scripts/stats.py ../iati-publisher-docs
    python scripts/stats.py ../iati-publisher-docs --language fr
    python scripts/stats.py ../iati-publisher-docs --verbose
"""

import argparse
import sys

from translation_lib import (
    LANGUAGE_NAMES,
    configure_project,
    detect_languages,
    get_po_files,
    is_locked,
    load_po_file,
)
from translation_lib import config as tl_config
from translation_lib.po_utils import get_needs_review


def show_stats(languages: list[str], verbose: bool = False) -> None:
    """Display translation statistics."""
    print("Translation Statistics")
    print("=" * 60)
    print(f"Project: {tl_config.PROJECT_ROOT}")

    for lang in languages:
        lang_name = LANGUAGE_NAMES.get(lang, lang)
        print(f"\n{lang_name} ({lang}):")
        print("-" * 40)

        po_files = get_po_files(lang)
        if not po_files:
            print("  No .po files found")
            continue

        total_translated = 0
        total_untranslated = 0
        total_fuzzy = 0
        total_needs_review = 0
        total_locked = 0
        file_stats = []

        for po_path in po_files:
            po = load_po_file(po_path)
            translated = len(po.translated_entries())
            untranslated = len(po.untranslated_entries())
            fuzzy = len(po.fuzzy_entries())
            # Fuzzy entries the tool itself flagged because it could not bring
            # them up to standard (vs. incoming carry-over still to be processed).
            needs_review = sum(
                1 for e in po.fuzzy_entries() if e.msgid and get_needs_review(e)
            )
            locked = sum(1 for e in po if e.msgid and is_locked(e))
            total = translated + untranslated

            total_translated += translated
            total_untranslated += untranslated
            total_fuzzy += fuzzy
            total_needs_review += needs_review
            total_locked += locked

            if verbose or untranslated > 0 or fuzzy > 0 or locked > 0:
                file_stats.append({
                    "name": po_path.name,
                    "translated": translated,
                    "untranslated": untranslated,
                    "fuzzy": fuzzy,
                    "locked": locked,
                    "total": total,
                })

        # Summary
        grand_total = total_translated + total_untranslated
        if grand_total > 0:
            percentage = (total_translated / grand_total) * 100
            print(f"  Overall: {total_translated}/{grand_total} ({percentage:.1f}%) translated")
            if total_fuzzy > 0:
                print(f"  Fuzzy (needs review): {total_fuzzy}")
                if total_needs_review > 0:
                    print(f"    of which {total_needs_review} flagged by the tool "
                          f"as unable to reach the standard")
            if total_locked > 0:
                print(f"  Locked (manual edits): {total_locked}")
            if total_untranslated > 0:
                print(f"  Missing: {total_untranslated}")
        else:
            print("  No translatable strings found")

        # Per-file breakdown
        if file_stats:
            print(f"\n  Per-file breakdown:")
            for stats in file_stats:
                status_parts = []
                if stats["untranslated"] > 0:
                    status_parts.append(f"{stats['untranslated']} missing")
                if stats["fuzzy"] > 0:
                    status_parts.append(f"{stats['fuzzy']} fuzzy")
                if stats["locked"] > 0:
                    status_parts.append(f"{stats['locked']} locked")
                if status_parts:
                    status = f" ({', '.join(status_parts)})"
                elif verbose:
                    status = " (complete)"
                else:
                    status = ""

                pct = (stats["translated"] / stats["total"] * 100) if stats["total"] > 0 else 100
                print(f"    {stats['name']}: {stats['translated']}/{stats['total']} ({pct:.0f}%){status}")

    # Multi-language summary
    if len(languages) > 1:
        print(f"\n{'=' * 60}")
        print("All Languages Summary:")
        print("=" * 60)
        for lang in languages:
            lang_name = LANGUAGE_NAMES.get(lang, lang)
            total_missing = 0
            for po_path in get_po_files(lang):
                po = load_po_file(po_path)
                total_missing += len(po.untranslated_entries())

            if total_missing == 0:
                print(f"  {lang_name}: All strings translated!")
            else:
                print(f"  {lang_name}: {total_missing} strings need translation")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Show translation statistics",
    )
    parser.add_argument(
        "project_path",
        help="Path to the target documentation project",
    )
    parser.add_argument(
        "--language", "-l",
        help="Show stats for specific language only",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show detailed per-file breakdown for all files",
    )

    args = parser.parse_args()

    # Configure target project
    try:
        configure_project(args.project_path)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        return 1

    # Default to detected languages (stats only makes sense for existing languages)
    if args.language:
        languages = [args.language]
    else:
        languages = detect_languages()
        if not languages:
            print(f"No locale directories found in {tl_config.LOCALE_DIR}")
            print("Run translate.py first to create translations.")
            return 1

    show_stats(languages, verbose=args.verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main())
