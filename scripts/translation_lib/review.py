"""
Translation review logic shared by translate.py and review.py.

Provides per-file review and site-wide cross-file consistency review.
"""

import json
import re

import polib

from .checks import fix_url_language_codes, validate_revision
from .config import LANGUAGE_NAMES, SITE_REVIEW_CHUNK_SIZE, TranslationConfig
from .fingerprint import is_review_current, standard_version
from .formatting import format_diff, location, truncate
from .llm_utils import call_llm, parse_json_response
from .po_utils import get_po_files, is_locked, load_po_file, save_po_files, strip_obsolete
from .prompts import (
    REVIEW_ERROR_CATEGORIES,
    build_completeness_prompt,
    build_review_prompt,
    build_site_review_prompt,
)


def _span_present(span: str, text: str) -> bool:
    """True if `span` appears in `text`, ignoring case and whitespace runs.

    Used to verify the reviewer cited real evidence quoted from the source,
    rather than fabricating a justification for a stylistic change.
    """
    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", s).strip().lower()

    return norm(span) in norm(text)


def assess_completeness(
    client,
    entry: polib.POEntry,
    language: str,
    config: TranslationConfig,
) -> tuple[str, str]:
    """Check one translation for dropped source meaning, and fix it if possible.

    This is the LLM half of the truncation guard: cheap deterministic checks
    cannot tell whether a translation that is merely *short* has actually lost a
    clause. It is meant to be called only on entries that
    ``checks.is_truncation_suspect`` has already singled out, so its cost is
    bounded to the few entries worth a look.

    Like the per-file reviewer, it demands evidence — the omitted words must be
    quoted verbatim from the source — so it cannot fabricate an omission and
    demote a sound translation. Returns one of:

    - ``("complete", "")``          — no omission found (or none it could prove)
    - ``("revised", revised_text)`` — an omission was found and a safe, complete
                                      revision is ready to apply
    - ``("incomplete", reason)``    — an omission was found but could not be
                                      safely repaired; the caller should demote
                                      the entry to fuzzy and flag it
    """
    prompt = build_completeness_prompt(entry.msgid, entry.msgstr, language)
    result = call_llm(client, prompt, json_mode=True)

    try:
        data = parse_json_response(result)
    except json.JSONDecodeError:
        # Unparseable verdict: don't block on it — the deterministic checks
        # still gate this entry. Treat as complete rather than demote.
        return ("complete", "")

    if not isinstance(data, dict) or data.get("complete") is not False:
        return ("complete", "")

    # An omission is only credible if the model can quote real source words.
    missing = (data.get("missing_source_span") or "").strip()
    if not missing or not _span_present(missing, entry.msgid):
        return ("complete", "")

    reason = f"incomplete translation: source text omitted (\"{truncate(missing)}\")"

    # Prefer to repair rather than demote: apply the model's complete revision
    # if it is a real change that introduces no new deterministic problems.
    revised = data.get("revised")
    if isinstance(revised, str) and revised.strip():
        revised = fix_url_language_codes(revised, language)
        if revised != entry.msgstr:
            new_issues = validate_revision(
                entry.msgid, entry.msgstr, revised, language, config
            )
            if not new_issues:
                return ("revised", revised)

    return ("incomplete", reason)


def review_po_files(
    client,
    language: str,
    config: TranslationConfig,
    *,
    apply: bool = True,
    dry_run: bool = False,
    show_header: bool = True,
    verbose: bool = True,
) -> tuple[int, int]:
    """Review translations per file. Returns (issues_found, revisions_applied)."""
    lang_name = LANGUAGE_NAMES.get(language, language)
    if show_header:
        print(f"\nReviewing {lang_name} translations for consistency...")

    total_issues = 0
    total_applied = 0
    std_version = standard_version(config, language)

    for po_path in get_po_files(language):
        po = load_po_file(po_path)
        translated = po.translated_entries()

        if not translated:
            continue

        if dry_run:
            print(f"  {po_path.name}: {len(translated)} translations to review")
            continue

        # Build review entries. Skip locked entries, and skip entries that
        # already passed review under the current standard (unchanged source,
        # translation, glossary and guidelines) — re-reviewing them just churns
        # correct translations across runs.
        review_entries = []
        entry_map: dict[int, polib.POEntry] = {}
        skipped_current = 0
        for i, entry in enumerate(translated):
            if is_locked(entry):
                continue
            if is_review_current(entry, language, std_version):
                skipped_current += 1
                continue
            review_entries.append({
                "index": i,
                "source": entry.msgid,
                "translation": entry.msgstr,
                "location": location(entry),
            })
            entry_map[i] = entry

        if not review_entries:
            continue

        if verbose:
            note = f", {skipped_current} unchanged" if skipped_current else ""
            print(f"  {po_path.name}: {len(review_entries)} translations to review{note}")

        prompt = build_review_prompt(
            review_entries, language, config, po_path.name
        )
        result = call_llm(client, prompt, json_mode=True)

        try:
            review_result = parse_json_response(result)
        except json.JSONDecodeError as e:
            print(f"    Warning: Could not parse review response: {e}")
            continue

        if review_result.get("status") != "revised":
            if verbose:
                print(f"    All approved")
            continue

        revisions = review_result.get("revisions", [])
        file_applied = 0

        for rev in revisions:
            idx = rev.get("index")
            revised = rev.get("revised")
            reason = rev.get("reason", rev.get("explanation", ""))

            if idx is None or revised is None or idx not in entry_map:
                continue

            # Enforce the revision contract: a genuine error names one of the
            # allowed categories and quotes the offending source span. Drop
            # revisions that can't supply real evidence — this is the main
            # brake on stylistic churn across re-runs.
            category = (rev.get("category") or "").strip().lower()
            source_span = (rev.get("source_span") or "").strip()
            if category not in REVIEW_ERROR_CATEGORIES:
                print(f"    SKIPPED [{idx}] revision without a valid error category")
                continue
            if not source_span or not _span_present(source_span, entry_map[idx].msgid):
                print(f"    SKIPPED [{idx}] revision did not cite a real source span")
                continue

            # Auto-fix URL language codes in the revision (reviewer often
            # copies /en/ URLs from the source text)
            revised = fix_url_language_codes(revised, language)

            if revised == entry_map[idx].msgstr:
                continue  # No actual change

            # Validate revision: reject if it introduces new issues
            new_issues = validate_revision(
                entry_map[idx].msgid, entry_map[idx].msgstr, revised, language, config
            )
            if new_issues:
                descs = "; ".join(i["description"] for i in new_issues)
                print(f"    REJECTED [{idx}] revision would introduce: {descs}")
                continue

            total_issues += 1
            diff = format_diff(entry_map[idx].msgstr, revised)
            print(f"    REVISED [{idx}] {diff}")
            if reason:
                print(f"      Reason: {reason}")

            if apply:
                entry_map[idx].msgstr = revised
                file_applied += 1

        if apply and file_applied > 0:
            strip_obsolete(po)
            po.save()
        total_applied += file_applied

    if total_issues == 0 and not dry_run:
        print(f"  All translations approved")

    return total_issues, total_applied


def review_site_wide(
    client,
    language: str,
    config: TranslationConfig,
    *,
    apply: bool = True,
    dry_run: bool = False,
    show_header: bool = True,
) -> tuple[int, int]:
    """Review all translations across files for cross-file consistency.

    Returns (issues_found, revisions_applied).
    """
    lang_name = LANGUAGE_NAMES.get(language, language)
    if show_header:
        print(f"\nSite-wide consistency review for {lang_name}...")

    # Collect all translations across files
    all_entries: list[dict] = []
    entry_map: dict[int, tuple[polib.POFile, polib.POEntry]] = {}
    global_idx = 0
    std_version = standard_version(config, language)
    stale_exists = False

    for po_path in get_po_files(language):
        po = load_po_file(po_path)
        for entry in po.translated_entries():
            if is_locked(entry):
                continue
            all_entries.append({
                "index": global_idx,
                "file_name": po_path.name,
                "source": entry.msgid,
                "translation": entry.msgstr,
                "location": location(entry),
            })
            entry_map[global_idx] = (po, entry)
            global_idx += 1
            if not is_review_current(entry, language, std_version):
                stale_exists = True

    if not all_entries:
        print("  No translations found.")
        return 0, 0

    # Cross-file consistency depends on the full set, so we don't drop individual
    # entries from the prompt. But if every translation already passed review
    # under the current standard, there is nothing new to compare — skip the
    # phase entirely rather than re-reviewing an unchanged site.
    if not stale_exists and not dry_run:
        print("  All translations unchanged since last review — skipping.")
        return 0, 0

    file_names = {e["file_name"] for e in all_entries}
    print(f"  {len(all_entries)} translations across {len(file_names)} files")

    if dry_run:
        num_chunks = (len(all_entries) + SITE_REVIEW_CHUNK_SIZE - 1) // SITE_REVIEW_CHUNK_SIZE
        print(f"  Would review in {num_chunks} chunk(s)")
        return 0, 0

    # Chunk and process
    chunks = [
        all_entries[i : i + SITE_REVIEW_CHUNK_SIZE]
        for i in range(0, len(all_entries), SITE_REVIEW_CHUNK_SIZE)
    ]

    total_issues = 0
    total_applied = 0
    files_to_save: list[polib.POFile] = []

    for chunk_num, chunk in enumerate(chunks, 1):
        if len(chunks) > 1:
            print(f"  Reviewing chunk {chunk_num}/{len(chunks)} ({len(chunk)} entries)...")

        prompt = build_site_review_prompt(chunk, language, config)
        result = call_llm(client, prompt, json_mode=True)

        try:
            review_result = parse_json_response(result)
        except json.JSONDecodeError as e:
            print(f"    Warning: Could not parse chunk {chunk_num} response: {e}")
            continue

        if review_result.get("status") == "approved":
            continue

        for issue in review_result.get("issues", []):
            desc = issue.get("description", "")
            occurrences = issue.get("occurrences", [])
            total_issues += len(occurrences)

            print(f"    Issue: {desc}")
            for occ in occurrences:
                idx = occ.get("index")
                current = occ.get("current", "")
                revised = occ.get("revised", "")
                fname = occ.get("file_name", "")

                if current == revised:
                    continue

                if apply and idx is not None and revised and idx in entry_map:
                    po_file, po_entry = entry_map[idx]
                    # Auto-fix URL language codes in the revision
                    revised = fix_url_language_codes(revised, language)
                    # Validate revision before applying
                    new_issues = validate_revision(
                        po_entry.msgid, po_entry.msgstr, revised, language, config
                    )
                    if new_issues:
                        descs = "; ".join(i["description"] for i in new_issues)
                        print(f"      REJECTED {fname}[{idx}]: revision would introduce: {descs}")
                        continue
                    print(f"      {fname}[{idx}]: {format_diff(current, revised)}")
                    po_entry.msgstr = revised
                    files_to_save.append(po_file)
                    total_applied += 1
                else:
                    print(f"      {fname}[{idx}]: {format_diff(current, revised)}")

    # Save modified files (save_po_files de-duplicates by identity)
    save_po_files(files_to_save)

    if total_issues == 0:
        print("  All translations consistent")

    return total_issues, total_applied
