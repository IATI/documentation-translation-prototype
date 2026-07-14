"""
Unified entry quality assurance.

Combines deterministic auto-fixes, checks, and LLM-driven correction
into a single ensure_entry_quality() function.
"""

import json
from dataclasses import dataclass, field

import polib

from .checks import auto_fix_entry, run_all_checks, validate_revision
from .config import TranslationConfig
from .llm_utils import call_llm, parse_json_response
from .prompts import build_correction_prompt, build_glossary_focus_prompt


@dataclass
class QualityResult:
    """Result of a quality assurance pass on a single entry."""
    status: str  # "passed", "fixed", "unfixable"
    fixes_applied: list[str] = field(default_factory=list)
    remaining_issues: list[dict] = field(default_factory=list)


def ensure_entry_quality(
    client,
    entry: polib.POEntry,
    language: str,
    config: TranslationConfig,
    *,
    max_attempts: int = 2,
) -> QualityResult:
    """Process a single PO entry through the fix-check-correct loop.

    Flow:
    1. Auto-fix deterministic issues (URL language codes)
    2. Check for remaining issues
    3. If issues remain, ask the LLM to correct the specific problems
    4. Validate the correction doesn't introduce new issues
    5. Repeat up to max_attempts times, then flag as unfixable
    """
    if not entry.msgid or not entry.msgstr:
        return QualityResult(status="passed")

    fixes_applied: list[str] = []

    # Step 1: Deterministic auto-fixes
    fixes = auto_fix_entry(entry, language)
    fixes_applied.extend(fixes)

    # Step 2: Check for remaining issues
    issues = run_all_checks(entry, language, config)
    if not issues:
        status = "fixed" if fixes_applied else "passed"
        return QualityResult(status=status, fixes_applied=fixes_applied)

    # Step 3: LLM correction loop
    for attempt in range(max_attempts):
        system_msg, user_msg = build_correction_prompt(
            entry.msgid, entry.msgstr, issues, language, config
        )
        raw = call_llm(
            client, user_msg, system=system_msg, json_mode=True
        )

        try:
            data = parse_json_response(raw)
        except json.JSONDecodeError:
            data = None
        corrected = data.get("translation") if isinstance(data, dict) else None

        # If we couldn't extract a usable correction, don't overwrite the
        # existing translation with raw output — just try again.
        if not isinstance(corrected, str) or not corrected.strip():
            continue

        # Step 4: Validate the correction doesn't introduce new issues
        new_issues = validate_revision(
            entry.msgid, entry.msgstr, corrected, language, config
        )
        if new_issues:
            # Correction made things worse — reject it
            continue

        # Step 5: Check if the correction actually fixed the issues
        test_entry = polib.POEntry(msgid=entry.msgid, msgstr=corrected)
        remaining = run_all_checks(test_entry, language, config)
        if not remaining:
            # All issues resolved — apply the correction
            fixes_applied.append(
                f"LLM correction (attempt {attempt + 1}): "
                + "; ".join(i["description"] for i in issues)
            )
            entry.msgstr = corrected
            return QualityResult(status="fixed", fixes_applied=fixes_applied)

        # Some issues remain — update for next attempt
        issues = remaining

    # Last resort: if every remaining issue is a missing glossary term, retry
    # once more with a prompt that drops every other rule and isolates the
    # model's attention on working the required term in. Mixed in with other
    # instructions, a glossary miss is easy for the model to skip past; alone,
    # with a worked example of grammatical adaptation, it's usually mechanical.
    if issues and all(issue["type"] == "glossary_term" for issue in issues):
        system_msg, user_msg = build_glossary_focus_prompt(
            entry.msgid, entry.msgstr, issues, language, config
        )
        raw = call_llm(client, user_msg, system=system_msg, json_mode=True)

        try:
            data = parse_json_response(raw)
        except json.JSONDecodeError:
            data = None
        corrected = data.get("translation") if isinstance(data, dict) else None

        if isinstance(corrected, str) and corrected.strip():
            new_issues = validate_revision(
                entry.msgid, entry.msgstr, corrected, language, config
            )
            if not new_issues:
                test_entry = polib.POEntry(msgid=entry.msgid, msgstr=corrected)
                remaining = run_all_checks(test_entry, language, config)
                if not remaining:
                    fixes_applied.append(
                        "LLM correction (glossary-focused retry): "
                        + "; ".join(i["description"] for i in issues)
                    )
                    entry.msgstr = corrected
                    return QualityResult(status="fixed", fixes_applied=fixes_applied)
                issues = remaining

    # Exhausted attempts
    return QualityResult(
        status="unfixable",
        fixes_applied=fixes_applied,
        remaining_issues=issues,
    )
