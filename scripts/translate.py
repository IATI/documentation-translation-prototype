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
import time
import traceback
from datetime import datetime
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
    describe_api_error,
    get_po_files,
    is_locked,
    load_po_file,
    parse_json_response,
    save_po_files,
    scaffold_language,
    strip_obsolete,
)
from translation_lib import config as tl_config
from translation_lib.checks import is_truncation_suspect, run_all_checks
from translation_lib.fingerprint import (
    entry_fingerprint,
    is_review_current,
    needs_review_current,
    standard_version,
)
from translation_lib.po_utils import (
    clear_needs_review,
    get_needs_review,
    set_needs_review,
    set_review_fingerprint,
)
from translation_lib.formatting import fmt_duration, format_diff, location, truncate
from translation_lib.prompts import build_translation_prompt
from translation_lib.quality import ensure_entry_quality
from translation_lib.review import (
    assess_completeness,
    review_po_files,
    review_site_wide,
)
from translation_lib.runlog import RunLog

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
        env=tl_config.sphinx_env(),
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
            env=tl_config.sphinx_env(),
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
    *,
    verbose: bool = False,
    dry_run: bool = False,
) -> tuple[int, int, int]:
    """Translate all untranslated strings for a language.

    Each entry is translated, then immediately checked for objective issues
    (truncation, formatting, URL codes) via ensure_entry_quality().

    Returns (count_of_new_translations, count_of_auto_fixes, count_skipped).
    """
    # Gather untranslated entries across all files up front, so progress can be
    # reported against the whole job rather than a counter that resets per file.
    file_work: list[tuple[Path, polib.POFile, list]] = []
    total = 0
    for po_path in get_po_files(language):
        po = load_po_file(po_path)
        untranslated = [
            e for e in po.untranslated_entries()
            if len(e.msgid.strip()) >= 2 and not is_locked(e)
        ]
        if untranslated:
            file_work.append((po_path, po, untranslated))
            total += len(untranslated)

    if total == 0:
        print("  No untranslated strings found")
        return 0, 0, 0

    print(f"  {total} strings to translate across {len(file_work)} file(s)")

    if dry_run:
        if verbose:
            n = 0
            for _po_path, _po, untranslated in file_work:
                for entry in untranslated:
                    n += 1
                    print(f"    NEW [{n}/{total}] \"{truncate(entry.msgid)}\"")
        return total, 0, 0

    start = time.monotonic()
    progress_step = max(1, total // 20)  # report roughly every 5%

    done = 0
    total_new = 0
    total_fixes = 0
    total_skipped = 0

    for _po_path, po, untranslated in file_work:
        file_changed = False
        for entry in untranslated:
            done += 1
            src_display = truncate(entry.msgid)

            system_msg, user_msg = build_translation_prompt(entry.msgid, language, config)
            raw = call_llm(client, user_msg, system=system_msg, json_mode=True)

            try:
                data = parse_json_response(raw)
            except json.JSONDecodeError:
                data = None
            translation = data.get("translation") if isinstance(data, dict) else None

            # If we couldn't extract a usable translation, leave the entry
            # untranslated rather than writing raw LLM output into the PO file.
            # A re-run will retry it.
            if not isinstance(translation, str) or not translation.strip():
                total_skipped += 1
                print(f"    SKIPPED [{done}/{total}] \"{src_display}\" — "
                      f"no usable translation in LLM response")
                continue

            entry.msgstr = translation
            file_changed = True

            if verbose:
                print(f"    NEW [{done}/{total}] \"{src_display}\" -> \"{truncate(translation)}\"")

            # Immediate quality check: fix URLs, catch truncation/formatting
            qr = ensure_entry_quality(client, entry, language, config)
            total_fixes += len(qr.fixes_applied)
            for fix in qr.fixes_applied:
                print(f"      FIX: {fix}")
            for issue in qr.remaining_issues:
                print(f"      WARNING: {issue['description']}")

            total_new += 1

            # Compact progress milestone (~5% steps) when not in verbose mode.
            if not verbose and (done % progress_step == 0 or done == total):
                pct = done / total * 100
                print(f"    {done}/{total} ({pct:.0f}%) · {fmt_duration(time.monotonic() - start)}")

        if file_changed:
            strip_obsolete(po)
            po.save()

    return total_new, total_fixes, total_skipped


# -----------------------------------------------------------------------------
# Step 4: Review — see translation_lib/review.py
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Step 5: Refresh fuzzy entries
# -----------------------------------------------------------------------------


def _retranslate_entry(
    client: Mistral | None,
    entry: polib.POEntry,
    language: str,
    config: TranslationConfig,
) -> str | None:
    """Translate an entry from scratch from its current source.

    Returns the new translation, or None if the LLM gave nothing usable.
    """
    system_msg, user_msg = build_translation_prompt(entry.msgid, language, config)
    raw = call_llm(client, user_msg, system=system_msg, json_mode=True)
    try:
        data = parse_json_response(raw)
    except json.JSONDecodeError:
        data = None
    translation = data.get("translation") if isinstance(data, dict) else None
    if not isinstance(translation, str) or not translation.strip():
        return None
    return translation


def refresh_fuzzy_language(
    client: Mistral | None,
    language: str,
    config: TranslationConfig,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Re-translate fuzzy (carry-over) entries from their current source.

    A fuzzy entry's translation was carried over by msgmerge from an OLD,
    near-matching source, so it is suspect — it may describe the previous
    wording or even a different item. Rather than route it through a separate,
    weaker validation path, we re-translate it from the CURRENT source and clear
    the fuzzy flag. It then flows through the same review and settle phases as
    every other translation, so there is one quality path, not two.

    Entries already given up on under the current standard (see
    needs_review_current) are left fuzzy and untouched — re-attempting them
    would fail identically. Locked entries are left for manual handling.

    Returns (refreshed_count, deferred_count).
    """
    std_version = standard_version(config, language)
    to_refresh: list[tuple[Path, polib.POFile, polib.POEntry]] = []
    deferred = 0

    for po_path in get_po_files(language):
        po = load_po_file(po_path)
        for entry in po.fuzzy_entries():
            if not entry.msgid:
                continue
            if is_locked(entry):
                print(f"  WARNING: Locked entry is fuzzy — review manually: "
                      f"{po_path.name}: \"{truncate(entry.msgid)}\"")
                continue
            if needs_review_current(entry, language, std_version):
                deferred += 1
                continue
            to_refresh.append((po_path, po, entry))

    deferred_note = (
        f", {deferred} left flagged for manual review" if deferred else ""
    )

    if not to_refresh:
        print(f"  No fuzzy entries to refresh{deferred_note}")
        return 0, deferred

    print(f"  {len(to_refresh)} fuzzy entries to re-translate{deferred_note}")

    if dry_run:
        for po_path, _po, entry in to_refresh:
            print(f"    FUZZY {po_path.name}: \"{truncate(entry.msgid)}\"")
        return 0, deferred

    refreshed = 0
    files_to_save: list[polib.POFile] = []
    for po_path, po, entry in to_refresh:
        new_translation = _retranslate_entry(client, entry, language, config)
        if new_translation is None:
            # Leave it fuzzy (so it renders as English); the settle phase will
            # report it as still needing review.
            print(f"    SKIPPED {po_path.name}: \"{truncate(entry.msgid)}\" — "
                  f"no usable translation, left fuzzy")
            continue
        print(f"    REFRESHED {po_path.name}: {format_diff(entry.msgstr, new_translation)}")
        entry.msgstr = new_translation
        entry.flags = [f for f in entry.flags if f != "fuzzy"]
        clear_needs_review(entry)
        files_to_save.append(po)
        refreshed += 1

        # Polish the fresh translation; the settle phase re-checks and fingerprints.
        qr = ensure_entry_quality(client, entry, language, config)
        for fix in qr.fixes_applied:
            print(f"      FIX: {fix}")

    save_po_files(files_to_save)

    return refreshed, deferred


# -----------------------------------------------------------------------------
# Progress helpers
# -----------------------------------------------------------------------------


def _phase_banner(lang_name: str, step: int, total: int, title: str) -> None:
    """Print a numbered phase header so the user can see where the run is."""
    print(f"\n[{lang_name}] Step {step}/{total}: {title}")


def _language_plan(lang: str) -> tuple[int, int, int, int]:
    """Return (to_translate, fuzzy, already_translated, locked) counts for a language."""
    to_translate = fuzzy = translated = locked = 0
    for po_path in get_po_files(lang):
        po = load_po_file(po_path)
        for entry in po:
            if not entry.msgid:
                continue
            if is_locked(entry):
                locked += 1
            elif "fuzzy" in entry.flags:
                fuzzy += 1
            elif entry.msgstr:
                translated += 1
            elif len(entry.msgid.strip()) >= 2:
                to_translate += 1
    return to_translate, fuzzy, translated, locked


# -----------------------------------------------------------------------------
# Per-language pipeline
# -----------------------------------------------------------------------------


def _process_language(
    client: Mistral | None,
    lang: str,
    config: TranslationConfig,
    args: argparse.Namespace,
) -> dict:
    """Run the full pipeline for one language and return its summary stats."""
    lang_name = LANGUAGE_NAMES.get(lang, lang)
    lang_start = time.monotonic()

    print(f"\n{'=' * 50}")
    print(f"{lang_name} ({lang})")
    print("=" * 50)

    # Work out which phases will actually run, so we can number them honestly.
    phases = ["Translating new strings", "Refreshing fuzzy translations"]
    if not args.skip_review:
        phases.append("Reviewing translations")
        phases.append("Site-wide consistency review")
    if not args.dry_run:
        phases.append("Final validation")
    total_phases = len(phases)
    step = 0

    # Phase: translate untranslated strings
    step += 1
    _phase_banner(lang_name, step, total_phases, "Translating new strings")
    new_count, auto_fixes, skipped = translate_language(
        client, lang, config, verbose=args.verbose, dry_run=args.dry_run
    )

    # Phase: refresh fuzzy carry-over by re-translating from the current source.
    # Runs before review so the refreshed text becomes an ordinary translation
    # that flows through the same review + validation as everything else, rather
    # than down a separate, weaker path.
    step += 1
    _phase_banner(lang_name, step, total_phases, "Refreshing fuzzy translations")
    fuzzy_refreshed, fuzzy_deferred = refresh_fuzzy_language(
        client, lang, config, dry_run=args.dry_run
    )

    # Phase: per-file review (with convergence loop)
    revision_count = 0
    if not args.skip_review:
        step += 1
        _phase_banner(lang_name, step, total_phases, "Reviewing translations")
        if args.dry_run:
            review_po_files(
                client, lang, config, apply=True, dry_run=True,
                show_header=False, verbose=args.verbose,
            )
        else:
            for pass_num in range(1, MAX_REVIEW_PASSES + 1):
                _, pass_revisions = review_po_files(
                    client, lang, config, apply=True, dry_run=False,
                    show_header=False, verbose=args.verbose,
                )
                revision_count += pass_revisions
                if pass_revisions == 0:
                    break
                if pass_num < MAX_REVIEW_PASSES:
                    print(f"  Pass {pass_num}: {pass_revisions} revisions applied, re-reviewing...")

        # Phase: site-wide consistency review across files.
        step += 1
        _phase_banner(lang_name, step, total_phases, "Site-wide consistency review")
        _, site_revisions = review_site_wide(
            client, lang, config, apply=True, dry_run=args.dry_run, show_header=False,
        )
        revision_count += site_revisions

    # Phase: final validation (settle) — re-check every translation, fix what we
    # can, and record a verdict per entry. An entry that still fails a check is
    # demoted to fuzzy and flagged NEEDS-REVIEW: it renders as the English source
    # (never a known-bad translation) and is reported for a human, and a
    # re-run skips re-attempting it until its source or the standard changes.
    # Entries that pass record a review fingerprint so unchanged entries are
    # skipped next run.
    final_fixes = 0
    final_fix_details: list[tuple[str, str, str, str]] = []
    remaining_issue_details: list[tuple[str, str, str]] = []
    demoted = 0
    if not args.dry_run:
        step += 1
        _phase_banner(lang_name, step, total_phases, "Final validation")
        std_version = standard_version(config, lang)

        entries_to_check: list[tuple[Path, polib.POFile, polib.POEntry]] = []
        for po_path in get_po_files(lang):
            po = load_po_file(po_path)
            for entry in po.translated_entries():
                if not is_locked(entry):
                    entries_to_check.append((po_path, po, entry))

        total_check = len(entries_to_check)
        print(f"  Validating {total_check} translations...")
        fstart = time.monotonic()
        fstep = max(1, total_check // 20)
        modified_pos: list[polib.POFile] = []

        for n, (po_path, po, entry) in enumerate(entries_to_check, 1):
            issues = run_all_checks(entry, lang, config)
            if issues:
                old_translation = entry.msgstr
                qr = ensure_entry_quality(client, entry, lang, config)
                if qr.fixes_applied:
                    final_fixes += 1
                    final_fix_details.append((
                        po_path.name, entry.msgid, old_translation, entry.msgstr,
                    ))
                    modified_pos.append(po)
                issues = qr.remaining_issues

            # LLM completeness check: for entries that pass the deterministic
            # checks but look truncated, confirm no source clause was dropped
            # (something length/formatting bounds can't detect). Gated to
            # unsettled entries and truncation suspects so the LLM cost stays
            # bounded to the few worth a look. A safe, complete revision is
            # applied; an unrepairable omission demotes the entry to fuzzy.
            if (
                not issues
                and not args.skip_review
                and not is_review_current(entry, lang, std_version)
                and is_truncation_suspect(entry.msgid, entry.msgstr)
            ):
                outcome, payload = assess_completeness(client, entry, lang, config)
                if outcome == "revised":
                    old_translation = entry.msgstr
                    entry.msgstr = payload
                    final_fixes += 1
                    final_fix_details.append((
                        po_path.name, entry.msgid, old_translation, entry.msgstr,
                    ))
                    modified_pos.append(po)
                    issues = run_all_checks(entry, lang, config)
                elif outcome == "incomplete":
                    issues = [{"type": "incomplete", "description": payload}]

            if issues:
                # Couldn't reach the standard — demote to fuzzy and flag it.
                reason = "; ".join(i["description"] for i in issues)
                if "fuzzy" not in entry.flags:
                    entry.flags.append("fuzzy")
                set_needs_review(
                    entry, entry_fingerprint(entry, lang, std_version), reason
                )
                modified_pos.append(po)
                demoted += 1
                remaining_issue_details.append((po_path.name, entry.msgid, reason))
            elif not args.skip_review:
                # Passed under the current standard — fingerprint it so an
                # unchanged entry is skipped next run, and clear any stale
                # NEEDS-REVIEW marker. (Not done with --skip-review: the entry
                # was not LLM-reviewed, so we cannot call it settled.)
                fp = entry_fingerprint(entry, lang, std_version)
                if set_review_fingerprint(entry, fp):
                    modified_pos.append(po)

            if not args.verbose and total_check and (n % fstep == 0 or n == total_check):
                print(f"    {n}/{total_check} · {fmt_duration(time.monotonic() - fstart)}")

        save_po_files(modified_pos)
    else:
        # Dry run: count issues without fixing
        for po_path in get_po_files(lang):
            po = load_po_file(po_path)
            for entry in po.translated_entries():
                if is_locked(entry):
                    continue
                for issue in run_all_checks(entry, lang, config):
                    remaining_issue_details.append((
                        po_path.name, entry.msgid, issue["description"],
                    ))

    # Belt and suspenders: report any entry STILL fuzzy after the pipeline — the
    # ones deferred as unchanged failures, that could not be re-translated, or
    # locked-and-fuzzy. Entries demoted just above are already recorded; skip
    # those to avoid double-reporting. This is the guarantee that the run can
    # never report "all passed" while a sub-standard entry ships.
    if not args.dry_run:
        already = {(f, s) for f, s, _ in remaining_issue_details}
        for po_path in get_po_files(lang):
            po = load_po_file(po_path)
            for entry in po.fuzzy_entries():
                if not entry.msgid or (po_path.name, entry.msgid) in already:
                    continue
                nr = get_needs_review(entry)
                if is_locked(entry):
                    reason = "locked entry is fuzzy — resolve manually"
                elif nr and nr[1]:
                    reason = nr[1]
                else:
                    reason = "still marked fuzzy — needs manual review"
                remaining_issue_details.append((po_path.name, entry.msgid, reason))

    # Coverage stats
    total_translated = total_entries = total_locked = 0
    for po_path in get_po_files(lang):
        po = load_po_file(po_path)
        entries = [e for e in po if e.msgid]
        total_entries += len(entries)
        total_translated += len([e for e in entries if e.msgstr and "fuzzy" not in e.flags])
        total_locked += sum(1 for e in entries if is_locked(e))

    print(f"\n{lang_name} finished in {fmt_duration(time.monotonic() - lang_start)}")

    return {
        "name": lang_name,
        "new": new_count,
        "skipped": skipped,
        "revisions": revision_count,
        "auto_fixes": auto_fixes,
        "fuzzy_refreshed": fuzzy_refreshed,
        "fuzzy_deferred": fuzzy_deferred,
        "demoted": demoted,
        "final_fixes": final_fixes,
        "final_fix_details": final_fix_details,
        "remaining_issue_details": remaining_issue_details,
        "translated": total_translated,
        "total": total_entries,
        "locked": total_locked,
    }


def _print_summary(summary: dict[str, dict], dry_run: bool, run_start: float) -> None:
    """Print the end-of-run summary and overall verdict."""
    print("\n" + "=" * 50)
    print("Summary")
    print("=" * 50)

    for stats in summary.values():
        print(f"\n{stats['name']}:")
        if dry_run:
            print(f"  {stats['new']} strings to translate")
            remaining = stats.get("remaining_issue_details", [])
            if remaining:
                print(f"  {len(remaining)} existing translations have issues to fix")
            continue

        if stats["new"]:
            print(f"  {stats['new']} new translations")
        if stats.get("skipped"):
            print(f"  {stats['skipped']} strings could not be translated (will retry on next run)")
        if stats.get("auto_fixes"):
            print(f"  {stats['auto_fixes']} auto-fixes applied during translation")
        if stats["revisions"]:
            print(f"  {stats['revisions']} review revisions applied")
        if stats.get("fuzzy_refreshed"):
            print(f"  {stats['fuzzy_refreshed']} fuzzy translations refreshed")
        if stats.get("fuzzy_deferred"):
            print(f"  {stats['fuzzy_deferred']} fuzzy entries left flagged (unchanged since last failure)")
        if stats.get("demoted"):
            print(f"  {stats['demoted']} translations demoted to fuzzy (couldn't reach standard)")
        if stats.get("locked"):
            print(f"  {stats['locked']} locked entries skipped")
        if stats["total"] > 0:
            pct = stats["translated"] / stats["total"] * 100
            print(f"  Coverage: {stats['translated']}/{stats['total']} ({pct:.0f}%)")

        final_fix_details = stats.get("final_fix_details", [])
        if final_fix_details:
            print(f"  Final pass: {len(final_fix_details)} pre-existing translations corrected:")
            for file_name, source, old_trans, new_trans in final_fix_details:
                print(f"    {file_name}: \"{truncate(source)}\"")
                print(f"      {format_diff(old_trans, new_trans)}")

        remaining = stats.get("remaining_issue_details", [])
        if remaining:
            print(f"  Remaining: {len(remaining)} issues need manual review:")
            for file_name, source, description in remaining:
                print(f"    {file_name}: \"{truncate(source)}\"")
                print(f"      {description}")
        else:
            print("  Quality: all automated checks passed")

    if dry_run:
        return

    total_remaining = sum(len(s["remaining_issue_details"]) for s in summary.values())
    total_skipped = sum(s.get("skipped", 0) for s in summary.values())

    print(f"\nTotal time: {fmt_duration(time.monotonic() - run_start)}")
    print()
    if total_remaining or total_skipped:
        if total_remaining:
            print(f"{total_remaining} translation(s) need manual review — see details above.")
        if total_skipped:
            print(f"{total_skipped} string(s) could not be translated — re-run the tool to retry them.")
    else:
        print("All automated checks passed.")
        print("Note: this is machine translation — a fluent speaker should still")
        print("spot-check the results before publishing.")


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------


def _run_pipeline(
    args: argparse.Namespace,
    languages: list[str],
    api_key: str | None,
    run_start: float,
) -> int:
    """Run the whole pipeline. Assumes the project is already configured."""
    lang_display = ", ".join(LANGUAGE_NAMES.get(l, l) for l in languages)

    print("Documentation Translation")
    print("=" * 50)
    print(f"Project: {tl_config.PROJECT_ROOT}")
    print(f"Languages: {lang_display}")
    if args.dry_run:
        print("Mode: dry run — no files will be changed")
    print("Tip: run check_english.py first to catch source errors; --dry-run previews the work.")

    client = Mistral(api_key=api_key) if api_key else None
    config = TranslationConfig.load(args.config)

    # Scaffold locale directories for any missing languages (real runs only).
    if not args.dry_run:
        for lang in languages:
            lang_dir = tl_config.LOCALE_DIR / lang / "LC_MESSAGES"
            if not lang_dir.exists():
                scaffold_language(lang)

    # Steps 1 & 2: extract strings and update PO files
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

    # Overview / plan so the user can see the scale of the job up front.
    print("\nScanning current translation status...")
    print("Plan:")
    for lang in languages:
        to_translate, fuzzy, translated, locked = _language_plan(lang)
        bits = [
            f"{to_translate} to translate",
            f"{fuzzy} fuzzy to refresh",
            f"{translated} already translated",
        ]
        if locked:
            bits.append(f"{locked} locked")
        print(f"  {LANGUAGE_NAMES.get(lang, lang)}: " + ", ".join(bits))

    summary: dict[str, dict] = {}
    for lang in languages:
        summary[lang] = _process_language(client, lang, config, args)

    _print_summary(summary, args.dry_run, run_start)
    return 0


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
  python scripts/translate.py ../iati-publisher-docs --verbose              # Show every translation
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
        "--verbose", "-v",
        action="store_true",
        help="Show every translation as it happens (default: compact progress)",
    )
    parser.add_argument(
        "--log",
        type=Path,
        help="Path for the run log (default: translation-<timestamp>.log in the current directory)",
    )
    parser.add_argument(
        "--config", "-c",
        type=Path,
        help="Path to translation configuration file (overrides global config)",
    )

    args = parser.parse_args()
    run_start = time.monotonic()

    # Fail fast on a missing API key, before any filesystem side effects.
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key and not args.dry_run:
        print("Error: MISTRAL_API_KEY environment variable is not set.")
        print("Set it to a valid Mistral API key (a paid account is required),")
        print("or pass --dry-run to preview the work without calling the API.")
        return 1

    try:
        configure_project(args.project_path)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        return 1

    languages = [args.language] if args.language else SUPPORTED_LANGUAGES

    # Mirror output to a log file (real runs only) so the user has something
    # concrete to send to the developer if anything goes wrong.
    runlog: RunLog | None = None
    if not args.dry_run:
        log_path = args.log or (Path.cwd() / f"translation-{datetime.now():%Y%m%d-%H%M%S}.log")
        try:
            runlog = RunLog(log_path)
        except OSError as e:
            print(f"Warning: could not open log file ({e}); continuing without a log.")

    try:
        return _run_pipeline(args, languages, api_key, run_start)
    except KeyboardInterrupt:
        print("\nInterrupted. Any progress already saved to disk is kept — re-run to continue.")
        return 130
    except Exception as e:
        friendly = describe_api_error(e)
        print()
        if friendly:
            print(f"Error: {friendly}")
        else:
            print(f"Unexpected error: {type(e).__name__}: {e}")
        if runlog is not None:
            runlog.log_only("\n--- traceback ---\n" + traceback.format_exc())
            print(f"Technical details have been written to the log: {runlog.path}")
            print("If this keeps happening, send that log file to the tool's developer.")
        else:
            traceback.print_exc()
        return 1
    finally:
        if runlog is not None:
            print(f"\nLog saved to: {runlog.path}")
            runlog.close()


if __name__ == "__main__":
    sys.exit(main())
