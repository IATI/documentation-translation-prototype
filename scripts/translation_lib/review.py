"""
Translation review logic shared by translate.py and review.py.

Provides per-file review and site-wide cross-file consistency review.
"""

import json

import polib

from .checks import fix_url_language_codes, validate_revision
from .config import LANGUAGE_NAMES, SITE_REVIEW_CHUNK_SIZE, TranslationConfig
from .formatting import format_diff, location
from .llm_utils import call_reviewer_api, parse_json_response
from .po_utils import get_po_files, load_po_file
from .prompts import build_review_prompt, build_site_review_prompt


def review_po_files(
    client,
    language: str,
    config: TranslationConfig,
    *,
    apply: bool = True,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Review translations per file. Returns (issues_found, revisions_applied)."""
    lang_name = LANGUAGE_NAMES.get(language, language)
    print(f"\nReviewing {lang_name} translations for consistency...")

    total_issues = 0
    total_applied = 0

    for po_path in get_po_files(language):
        po = load_po_file(po_path)
        translated = po.translated_entries()

        if not translated:
            continue

        if dry_run:
            print(f"  {po_path.name}: {len(translated)} translations to review")
            continue

        # Build review entries
        review_entries = []
        entry_map: dict[int, polib.POEntry] = {}
        for i, entry in enumerate(translated):
            review_entries.append({
                "index": i,
                "source": entry.msgid,
                "translation": entry.msgstr,
                "location": location(entry),
            })
            entry_map[i] = entry

        print(f"  {po_path.name}: {len(translated)} translations checked")

        prompt = build_review_prompt(
            review_entries, language, config, po_path.name
        )
        result = call_reviewer_api(client, prompt)

        try:
            review_result = parse_json_response(result)
        except json.JSONDecodeError as e:
            print(f"    Warning: Could not parse review response: {e}")
            continue

        if review_result.get("status") != "revised":
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
            po.save()
        total_applied += file_applied

    if total_issues == 0:
        print(f"  All translations approved")

    return total_issues, total_applied


def review_site_wide(
    client,
    language: str,
    config: TranslationConfig,
    *,
    apply: bool = True,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Review all translations across files for cross-file consistency.

    Returns (issues_found, revisions_applied).
    """
    lang_name = LANGUAGE_NAMES.get(language, language)
    print(f"\nSite-wide consistency review for {lang_name}...")

    # Collect all translations across files
    all_entries: list[dict] = []
    entry_map: dict[int, tuple[polib.POFile, polib.POEntry]] = {}
    global_idx = 0

    for po_path in get_po_files(language):
        po = load_po_file(po_path)
        for entry in po.translated_entries():
            all_entries.append({
                "index": global_idx,
                "file_name": po_path.name,
                "source": entry.msgid,
                "translation": entry.msgstr,
                "location": location(entry),
            })
            entry_map[global_idx] = (po, entry)
            global_idx += 1

    if not all_entries:
        print("  No translations found.")
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
    files_to_save: dict[int, polib.POFile] = {}

    for chunk_num, chunk in enumerate(chunks, 1):
        if len(chunks) > 1:
            print(f"  Reviewing chunk {chunk_num}/{len(chunks)} ({len(chunk)} entries)...")

        prompt = build_site_review_prompt(chunk, language, config)
        result = call_reviewer_api(client, prompt)

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
                    files_to_save[id(po_file)] = po_file
                    total_applied += 1
                else:
                    print(f"      {fname}[{idx}]: {format_diff(current, revised)}")

    # Save modified files
    if files_to_save:
        saved = set()
        for po_id, po_file in files_to_save.items():
            if po_id not in saved:
                po_file.save()
                saved.add(po_id)

    if total_issues == 0:
        print("  All translations consistent")

    return total_issues, total_applied
