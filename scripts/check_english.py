#!/usr/bin/env python3
"""
Check English source text for errors before translation.

Runs deterministic checks (broken formatting, unbalanced markers) and
LLM-based checks (spelling, grammar) on the English source strings
extracted from POT files.  Read-only — does not modify any files.

Usage:
    python scripts/check_english.py ../iati-publisher-docs
    python scripts/check_english.py ../iati-publisher-docs --dry-run
    python scripts/check_english.py ../iati-publisher-docs --verbose

Environment Variables:
    MISTRAL_API_KEY: Your Mistral API key (not needed for --dry-run)
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

try:
    from mistralai import Mistral
except ImportError:
    print("Error: mistralai package not installed. Run: pip install mistralai")
    sys.exit(1)

from translation_lib import configure_project, call_llm, parse_json_response
from translation_lib import config as tl_config
from translation_lib.formatting import truncate

# Max entries per LLM call
CHUNK_SIZE = 40


# -----------------------------------------------------------------------------
# Deterministic checks on English source strings
# -----------------------------------------------------------------------------


def check_formatting(text: str) -> list[str]:
    """Check for broken or unbalanced formatting markers in source text."""
    issues = []

    # Check balanced bold markers (**)
    count = text.count("**")
    if count % 2 != 0:
        issues.append(f"Unbalanced bold markers (**): found {count} (expected even)")

    # Check balanced inline code (``)
    count = text.count("``")
    if count % 2 != 0:
        issues.append(f"Unbalanced inline code markers (``): found {count} (expected even)")

    # Check single backticks that aren't part of double backticks
    # (often a mistake — should be `` for RST inline code)
    stripped = text.replace("``", "")
    single_backticks = stripped.count("`")
    if single_backticks % 2 != 0:
        issues.append(f"Unbalanced single backtick (`): found {single_backticks} (expected even)")

    # Check RST link syntax: `text <url>`_ — look for malformed variants
    # Correct: `link text <https://example.com>`_
    # Broken: missing closing `_ or missing angle brackets
    partial_rst = re.findall(r'`[^`]+<https?://[^>]+>[^`]*(?!`_)', text)
    for match in partial_rst:
        if not match.endswith("`_") and "`_" not in text[text.index(match):text.index(match) + len(match) + 2]:
            pass  # Handled by the balanced backtick check above

    # Check for unclosed angle brackets in URLs
    open_angles = text.count("<")
    close_angles = text.count(">")
    if open_angles != close_angles:
        issues.append(f"Unbalanced angle brackets: {open_angles} '<' vs {close_angles} '>'")

    # Check for broken RST directives (common typos)
    broken_directives = re.findall(r'\.\. [a-z]+[^:]\s*\n', text)
    for d in broken_directives:
        directive = d.strip()
        if not directive.endswith("::"):
            issues.append(f"Possible broken RST directive (missing ::): '{directive}'")

    return issues


# -----------------------------------------------------------------------------
# LLM-based checks
# -----------------------------------------------------------------------------


def build_english_check_prompt(entries: list[dict]) -> str:
    """Build a prompt for checking English source text quality."""
    prompt_parts = [
        "You are a proofreader checking English documentation for errors.",
        "",
        "Check each entry for:",
        "1. Spelling errors (typos, misspelled words — e.g. 'appoved' instead of 'approved')",
        "2. Clear grammatical errors (e.g. 'should I populated?' instead of 'should I populate?')",
        "3. Broken formatting (unclosed markup, malformed URLs or links)",
        "",
        "Do NOT flag:",
        "- Style preferences, rewording suggestions, or alternative phrasings",
        "- Technical jargon or domain-specific terms (e.g. IATI, XLSX, CSV, XML)",
        "- Capitalisation choices (e.g. title case in headings)",
        "- reStructuredText or Markdown syntax that is valid",
        "- Sentence fragments — these are usually intentional list items or headings",
        "- Gerund phrases used as headings or list labels (e.g. 'Selecting activities...')",
        "- Punctuation style (Oxford comma, em-dash vs hyphen, smart quotes vs straight quotes)",
        "- Tense choices — the author chose the tense intentionally",
        "- Hyphenation choices (e.g. 'left hand' vs 'left-hand')",
        "",
        "=" * 60,
        "ENTRIES TO CHECK:",
        "",
    ]

    for entry in entries:
        prompt_parts.extend([
            f"[{entry['index']}] {entry['location']}",
            f"  TEXT: {entry['text']}",
            "",
        ])

    prompt_parts.extend([
        "=" * 60,
        "",
        "Respond with JSON. If all entries are fine:",
        '{"status": "ok", "issues": []}',
        "",
        "If there are errors:",
        '{"status": "issues_found", "issues": [',
        '  {"index": 0, "issue": "spelling", "description": "\'appoved\' should be \'approved\'", "suggestion": "corrected text"}',
        "]}",
        "",
        "IMPORTANT:",
        "- Only flag genuine errors, not style preferences",
        "- The 'suggestion' field should contain the corrected version of the problematic word/phrase, not the entire entry",
        "- Return ONLY valid JSON",
    ])

    return "\n".join(prompt_parts)


# -----------------------------------------------------------------------------
# POT file loading
# -----------------------------------------------------------------------------


def get_pot_files() -> list[Path]:
    """Get all .pot files from the build directory, sorted by name."""
    if tl_config.POT_DIR is None or not tl_config.POT_DIR.exists():
        return []
    return sorted(tl_config.POT_DIR.glob("*.pot"))


def load_pot_entries(pot_path: Path) -> list[dict]:
    """Load entries from a POT file. Returns list of dicts with index, text, location."""
    import polib

    pot = polib.pofile(str(pot_path))
    entries = []
    for entry in pot:
        if not entry.msgid or entry.msgid == "":
            continue
        loc = ""
        if entry.occurrences:
            ref_file, ref_line = entry.occurrences[0]
            name = ref_file.split("/")[-1] if "/" in ref_file else ref_file
            loc = f"{name}:{ref_line}"
        entries.append({
            "file": pot_path.name,
            "text": entry.msgid,
            "location": loc,
        })
    return entries


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check English source text for errors before translation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/check_english.py ../iati-publisher-docs
  python scripts/check_english.py ../iati-publisher-docs --dry-run
  python scripts/check_english.py ../iati-publisher-docs --verbose
        """,
    )
    parser.add_argument(
        "project_path",
        help="Path to the target documentation project",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be checked without calling the LLM",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show all entries being checked",
    )

    args = parser.parse_args()

    configure_project(args.project_path)

    print("English Source Text Check")
    print("=" * 60)
    print(f"Project: {tl_config.PROJECT_ROOT}")

    # Load POT files
    pot_files = get_pot_files()
    if not pot_files:
        print(f"\nNo .pot files found in {tl_config.POT_DIR}")
        print("Run 'sphinx-build -b gettext' first, or use translate.py which does this automatically.")
        return 1

    # Collect all entries
    all_entries: list[dict] = []
    for pot_path in pot_files:
        entries = load_pot_entries(pot_path)
        all_entries.extend(entries)

    print(f"Found {len(all_entries)} source strings across {len(pot_files)} files")

    # Phase 1: Deterministic checks
    print(f"\nFormatting checks...")
    det_issues = 0
    for entry in all_entries:
        issues = check_formatting(entry["text"])
        for issue in issues:
            print(f"  {entry['file']}: \"{truncate(entry['text'])}\"")
            print(f"    {issue}")
            det_issues += 1

    if det_issues == 0:
        print("  All formatting checks passed")
    else:
        print(f"  Found {det_issues} formatting issue(s)")

    # Phase 2: LLM checks
    if args.dry_run:
        num_chunks = (len(all_entries) + CHUNK_SIZE - 1) // CHUNK_SIZE
        print(f"\nDry run: would check {len(all_entries)} entries in {num_chunks} LLM call(s)")
        return 0 if det_issues == 0 else 1

    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        print("\nError: MISTRAL_API_KEY environment variable not set")
        print("Set it to enable spelling/grammar checks, or use --dry-run for formatting checks only.")
        return 1

    client = Mistral(api_key=api_key)

    # Number entries for the LLM
    for i, entry in enumerate(all_entries):
        entry["index"] = i

    chunks = [
        all_entries[i:i + CHUNK_SIZE]
        for i in range(0, len(all_entries), CHUNK_SIZE)
    ]

    print(f"\nSpelling and grammar checks ({len(chunks)} chunk(s))...")
    llm_issues = 0

    for chunk_num, chunk in enumerate(chunks, 1):
        if len(chunks) > 1:
            print(f"  Checking chunk {chunk_num}/{len(chunks)} ({len(chunk)} entries)...")

        prompt = build_english_check_prompt(chunk)
        result = call_llm(client, prompt, json_mode=True)

        try:
            response = parse_json_response(result)
        except json.JSONDecodeError as e:
            print(f"  Warning: Could not parse LLM response for chunk {chunk_num}: {e}")
            continue

        if response.get("status") == "ok":
            continue

        for issue in response.get("issues", []):
            idx = issue.get("index")
            description = issue.get("description", "")
            suggestion = issue.get("suggestion", "")

            if idx is None or idx < 0 or idx >= len(all_entries):
                continue

            entry = all_entries[idx]
            llm_issues += 1
            print(f"  {entry['file']}: \"{truncate(entry['text'])}\"")
            print(f"    {description}")
            if suggestion:
                print(f"    Suggestion: {suggestion}")

    if llm_issues == 0:
        print("  No spelling or grammar issues found")
    else:
        print(f"  Found {llm_issues} issue(s)")

    # Summary
    total = det_issues + llm_issues
    print(f"\n{'=' * 60}")
    print(f"Summary: {det_issues} formatting, {llm_issues} spelling/grammar ({total} total)")
    if total > 0:
        print("Fix these in the source .rst files before translating.")

    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
