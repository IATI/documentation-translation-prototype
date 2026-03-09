#!/usr/bin/env python3
"""
Translate documentation.

One-shot workflow: extract strings -> update PO files -> translate -> review -> handle fuzzy.

Usage:
    python scripts/translate.py ../iati-publisher-docs                       # All languages (default)
    python scripts/translate.py ../iati-publisher-docs --language fr          # Single language
    python scripts/translate.py ../iati-publisher-docs --dry-run              # Preview mode
    python scripts/translate.py ../iati-publisher-docs --skip-extract         # Skip Sphinx extraction steps
    python scripts/translate.py ../iati-publisher-docs --skip-review          # Skip review pass

Environment Variables:
    MISTRAL_API_KEY: Your Mistral API key (required unless --dry-run)
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

try:
    from mistralai import Mistral
except ImportError:
    print("Error: mistralai package not installed. Run: pip install mistralai")
    sys.exit(1)

import polib

from translation_lib import (
    LANGUAGE_NAMES,
    SUPPORTED_LANGUAGES,
    TranslationConfig,
    call_llm,
    configure_project,
    get_po_files,
    is_locked,
    load_po_file,
    parse_json_response,
    scaffold_language,
    strip_obsolete,
)
from translation_lib import config as tl_config
from translation_lib.checks import run_all_checks
from translation_lib.formatting import format_diff, location, truncate
from translation_lib.prompts import (
    build_fuzzy_validation_prompt,
    build_translation_prompt,
)
from translation_lib.quality import ensure_entry_quality
from translation_lib.review import review_po_files

# Maximum number of review passes before stopping (convergence loop)
MAX_REVIEW_PASSES = 2


# -----------------------------------------------------------------------------
# Step 1: Extract strings (sphinx-build -b gettext)
# -----------------------------------------------------------------------------


def extract_strings() -> bool:
    """Run sphinx-build to generate .pot files. Returns True on success."""
    print("Extracting strings from documentation...")
    result = subprocess.run(
        ["sphinx-build", "-b", "gettext", ".", "_build/locale"],
        cwd=str(tl_config.DOCS_DIR),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  Error running sphinx-build: {result.stderr}")
        return False

    pot_dir = tl_config.DOCS_DIR / "_build" / "locale"
    pot_files = list(pot_dir.glob("*.pot")) if pot_dir.exists() else []
    print(f"  Generated {len(pot_files)} .pot files")
    return True


# -----------------------------------------------------------------------------
# Step 2: Update PO files (sphinx-intl update)
# -----------------------------------------------------------------------------


def update_po_files(languages: list[str]) -> dict[str, int]:
    """Run sphinx-intl to sync .pot -> .po. Returns {lang: file_count}."""
    print("Updating .po files...")
    results = {}

    for lang in languages:
        result = subprocess.run(
            ["sphinx-intl", "update", "-p", "_build/locale", "-l", lang],
            cwd=str(tl_config.DOCS_DIR),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"  Error updating {lang}: {result.stderr}")
            results[lang] = 0
            continue

        file_count = len(get_po_files(lang))
        lang_name = LANGUAGE_NAMES.get(lang, lang)
        print(f"  {lang_name}: {file_count} files updated")
        results[lang] = file_count

    return results


# -----------------------------------------------------------------------------
# Step 3: Translate untranslated strings
# -----------------------------------------------------------------------------


def translate_language(
    client: Mistral | None,
    language: str,
    config: TranslationConfig,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Translate all untranslated strings for a language.

    Each entry is translated, then immediately checked for objective issues
    (truncation, formatting, URL codes) via ensure_entry_quality().

    Returns (count_of_new_translations, count_of_auto_fixes).
    """
    lang_name = LANGUAGE_NAMES.get(language, language)
    print(f"\nTranslating {lang_name} ({language})...")

    total_new = 0
    total_fixes = 0

    for po_path in get_po_files(language):
        po = load_po_file(po_path)
        untranslated = [e for e in po.untranslated_entries() if len(e.msgid.strip()) >= 2 and not is_locked(e)]

        if not untranslated:
            continue

        print(f"  {po_path.name}: {len(untranslated)} untranslated strings")
        file_new = 0

        for i, entry in enumerate(untranslated, 1):
            src_display = truncate(entry.msgid)

            if dry_run:
                print(f"    NEW [{i}/{len(untranslated)}] \"{src_display}\"")
                total_new += 1
                continue

            system_msg, user_msg = build_translation_prompt(
                entry.msgid, language, config
            )
            raw = call_llm(
                client, user_msg, system=system_msg, json_mode=True
            )

            try:
                data = parse_json_response(raw)
                translation = data.get("translation", raw)
            except json.JSONDecodeError:
                translation = raw

            entry.msgstr = translation
            trans_display = truncate(translation)
            print(f"    NEW [{i}/{len(untranslated)}] \"{src_display}\" -> \"{trans_display}\"")

            # Immediate quality check: fix URLs, catch truncation/formatting
            qr = ensure_entry_quality(client, entry, language, config)
            total_fixes += len(qr.fixes_applied)
            for fix in qr.fixes_applied:
                print(f"      FIX: {fix}")
            for issue in qr.remaining_issues:
                print(f"      WARNING: {issue['description']}")

            file_new += 1
            total_new += 1

        if not dry_run and file_new > 0:
            strip_obsolete(po)
            po.save()

    if total_new == 0:
        print(f"  No untranslated strings found")

    return total_new, total_fixes


# -----------------------------------------------------------------------------
# Step 4: Review — see translation_lib/review.py
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Step 5: Handle fuzzy entries
# -----------------------------------------------------------------------------


def handle_fuzzy_language(
    client: Mistral | None,
    language: str,
    config: TranslationConfig,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Validate fuzzy entries. Returns (approved_count, revised_count)."""
    lang_name = LANGUAGE_NAMES.get(language, language)

    # Collect all fuzzy entries across files
    fuzzy_entries: list[dict] = []
    fuzzy_map: dict[int, tuple[polib.POFile, polib.POEntry]] = {}
    global_idx = 0

    for po_path in get_po_files(language):
        po = load_po_file(po_path)
        for entry in po.fuzzy_entries():
            if is_locked(entry):
                print(f"  WARNING: Locked entry is fuzzy — review manually: "
                      f"{po_path.name}: \"{truncate(entry.msgid)}\"")
                continue
            fuzzy_entries.append({
                "index": global_idx,
                "file_name": po_path.name,
                "source": entry.msgid,
                "translation": entry.msgstr,
                "location": location(entry),
            })
            fuzzy_map[global_idx] = (po, entry)
            global_idx += 1

    if not fuzzy_entries:
        return 0, 0

    print(f"\nHandling fuzzy entries for {lang_name}...")
    print(f"  {len(fuzzy_entries)} fuzzy entries found")

    if dry_run:
        for fe in fuzzy_entries:
            print(f"    FUZZY [{fe['index']}] {fe['file_name']}: \"{truncate(fe['source'])}\"")
        return 0, 0

    # Send to LLM for validation
    prompt = build_fuzzy_validation_prompt(fuzzy_entries, language, config)
    result = call_llm(client, prompt, json_mode=True)

    try:
        parsed = parse_json_response(result)
    except json.JSONDecodeError as e:
        print(f"    Warning: Could not parse fuzzy validation response: {e}")
        return 0, 0

    # Handle both {"validations": [...]} and bare [...] formats
    if isinstance(parsed, list):
        validations = parsed
    else:
        validations = parsed.get("validations", [])

    approved = 0
    revised = 0
    files_to_save: dict[int, polib.POFile] = {}

    for validation in validations:
        idx = validation.get("index")
        decision = validation.get("decision", "")
        revised_text = validation.get("revised")
        reason = validation.get("reason", "")

        if idx is None or idx not in fuzzy_map:
            continue

        po_file, entry = fuzzy_map[idx]
        file_name = fuzzy_entries[idx]["file_name"]

        if decision == "approve":
            entry.flags = [f for f in entry.flags if f != "fuzzy"]
            files_to_save[id(po_file)] = po_file
            print(f"    APPROVED [{idx}] {file_name}: \"{truncate(entry.msgid)}\"")
            approved += 1
        elif decision == "revise" and revised_text:
            if revised_text == entry.msgstr:
                continue  # No actual change
            diff = format_diff(entry.msgstr, revised_text)
            entry.msgstr = revised_text
            entry.flags = [f for f in entry.flags if f != "fuzzy"]
            files_to_save[id(po_file)] = po_file
            print(f"    REVISED  [{idx}] {file_name}: {diff}")
            revised += 1
        else:
            continue

        # Quality check the approved/revised translation
        qr = ensure_entry_quality(client, entry, language, config)
        for fix in qr.fixes_applied:
            print(f"      FIX: {fix}")
        for issue in qr.remaining_issues:
            print(f"      WARNING: {issue['description']}")

    for po_file in files_to_save.values():
        strip_obsolete(po_file)
        po_file.save()

    return approved, revised


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Translate documentation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/translate.py ../iati-publisher-docs                       # All languages
  python scripts/translate.py ../iati-publisher-docs --language fr          # French only
  python scripts/translate.py ../iati-publisher-docs --dry-run              # Preview
  python scripts/translate.py ../iati-publisher-docs --skip-extract         # Skip Sphinx steps
  python scripts/translate.py ../iati-publisher-docs --skip-review          # Skip review pass
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
        "--dry-run",
        action="store_true",
        help="Preview without making changes",
    )
    parser.add_argument(
        "--skip-extract",
        action="store_true",
        help="Skip Sphinx string extraction and PO update steps",
    )
    parser.add_argument(
        "--skip-review",
        action="store_true",
        help="Skip the review pass after translation",
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

    # Scaffold locale directories for any missing languages
    for lang in languages:
        lang_dir = tl_config.LOCALE_DIR / lang / "LC_MESSAGES"
        if not lang_dir.exists():
            scaffold_language(lang)

    lang_display = ", ".join(LANGUAGE_NAMES.get(l, l) for l in languages)

    print("Documentation Translation")
    print("=" * 40)
    print(f"Project: {tl_config.PROJECT_ROOT}")
    print(f"Languages: {lang_display}")

    # Check API key
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key and not args.dry_run:
        print("Error: MISTRAL_API_KEY environment variable not set")
        return 1

    client = Mistral(api_key=api_key) if api_key else None
    config = TranslationConfig.load(args.config)

    # Step 1 & 2: Extract and update PO files
    if not args.skip_extract and not args.dry_run:
        if not extract_strings():
            return 1
        update_po_files(languages)

    # Strip obsolete entries from all PO files
    if not args.dry_run:
        total_obsolete = 0
        for lang in languages:
            for po_path in get_po_files(lang):
                po = load_po_file(po_path)
                removed = strip_obsolete(po)
                if removed:
                    po.save()
                    total_obsolete += removed
        if total_obsolete:
            print(f"Removed {total_obsolete} obsolete entries")

    # Steps 3-5: Translate, review, handle fuzzy for each language
    summary: dict[str, dict] = {}

    for lang in languages:
        lang_name = LANGUAGE_NAMES.get(lang, lang)

        # Step 3: Translate (with inline quality checks per entry)
        new_count, auto_fixes = translate_language(
            client, lang, config, dry_run=args.dry_run
        )

        # Step 4: Review with convergence loop
        revision_count = 0
        if not args.skip_review:
            for pass_num in range(1, MAX_REVIEW_PASSES + 1):
                _, pass_revisions = review_po_files(
                    client, lang, config, apply=True, dry_run=args.dry_run
                )
                revision_count += pass_revisions
                if pass_revisions == 0 or args.dry_run:
                    break
                if pass_num < MAX_REVIEW_PASSES:
                    print(f"\n  Review pass {pass_num}: {pass_revisions} revisions, re-reviewing...")

        # Step 5: Handle fuzzy
        approved, fuzzy_revised = handle_fuzzy_language(
            client, lang, config, dry_run=args.dry_run
        )

        # Coverage stats
        total_translated = 0
        total_entries = 0
        total_locked = 0
        for po_path in get_po_files(lang):
            po = load_po_file(po_path)
            entries = [e for e in po if e.msgid]
            total_entries += len(entries)
            total_translated += len([e for e in entries if e.msgstr and "fuzzy" not in e.flags])
            total_locked += sum(1 for e in entries if is_locked(e))

        # Final validation: attempt to fix remaining issues
        final_fixes = 0
        final_fix_details = []  # (file, source, old_translation, new_translation)
        remaining_issue_details = []  # (file, source, description)
        if not args.dry_run:
            modified_pos: dict[str, polib.POFile] = {}
            for po_path in get_po_files(lang):
                po = load_po_file(po_path)
                for entry in po.translated_entries():
                    if is_locked(entry):
                        continue
                    issues = run_all_checks(entry, lang, config)
                    if not issues:
                        continue
                    old_translation = entry.msgstr
                    qr = ensure_entry_quality(client, entry, lang, config)
                    if qr.fixes_applied:
                        final_fixes += 1
                        final_fix_details.append((
                            po_path.name, entry.msgid,
                            old_translation, entry.msgstr,
                        ))
                        modified_pos[str(po_path)] = po
                    for issue in qr.remaining_issues:
                        remaining_issue_details.append((
                            po_path.name, entry.msgid, issue["description"],
                        ))
            for po in modified_pos.values():
                strip_obsolete(po)
                po.save()
        else:
            # Dry run: just count issues without fixing
            for po_path in get_po_files(lang):
                po = load_po_file(po_path)
                for entry in po.translated_entries():
                    if is_locked(entry):
                        continue
                    issues = run_all_checks(entry, lang, config)
                    for issue in issues:
                        remaining_issue_details.append((
                            po_path.name, entry.msgid, issue["description"],
                        ))

        summary[lang] = {
            "name": lang_name,
            "new": new_count,
            "revisions": revision_count,
            "auto_fixes": auto_fixes,
            "fuzzy_approved": approved,
            "fuzzy_revised": fuzzy_revised,
            "final_fixes": final_fixes,
            "final_fix_details": final_fix_details,
            "remaining_issue_details": remaining_issue_details,
            "translated": total_translated,
            "total": total_entries,
            "locked": total_locked,
        }

    # Print summary
    print("\n" + "=" * 40)
    print("Summary")
    print("=" * 40)

    any_issues = False
    for lang, stats in summary.items():
        name = stats["name"]
        print(f"\n{name}:")
        if args.dry_run:
            print(f"  {stats['new']} strings to translate")
        else:
            if stats["new"]:
                print(f"  {stats['new']} new translations")
            if stats.get("auto_fixes"):
                print(f"  {stats['auto_fixes']} auto-fixes applied during translation")
            if stats["revisions"]:
                print(f"  {stats['revisions']} review revisions applied")
            if stats["fuzzy_approved"] or stats["fuzzy_revised"]:
                print(
                    f"  {stats['fuzzy_approved']} fuzzy approved, "
                    f"{stats['fuzzy_revised']} fuzzy revised"
                )
            if stats.get("locked"):
                print(f"  {stats['locked']} locked entries skipped")
            if stats["total"] > 0:
                pct = stats["translated"] / stats["total"] * 100
                print(f"  Coverage: {stats['translated']}/{stats['total']} ({pct:.0f}%)")

            # Show final-pass fixes
            final_fix_details = stats.get("final_fix_details", [])
            if final_fix_details:
                print(f"  Final pass: {len(final_fix_details)} pre-existing translations corrected:")
                for file_name, source, old_trans, new_trans in final_fix_details:
                    print(f"    {file_name}: \"{truncate(source)}\"")
                    print(f"      {format_diff(old_trans, new_trans)}")

            # Show remaining issues
            remaining = stats.get("remaining_issue_details", [])
            if remaining:
                print(f"  Remaining: {len(remaining)} issues need manual review:")
                for file_name, source, description in remaining:
                    print(f"    {file_name}: \"{truncate(source)}\"")
                    print(f"      {description}")
                any_issues = True
            else:
                print(f"  Quality: all checks passed")

    if not args.dry_run:
        print()
        if any_issues:
            total_remaining = sum(
                len(s["remaining_issue_details"]) for s in summary.values()
            )
            print(f"{total_remaining} translations need manual review. See details above.")
        else:
            print("All translations complete and verified. Ready to publish.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
