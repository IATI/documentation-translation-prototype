"""
LLM API utilities for translation and review.
"""

import json
import re
import time

from mistralai import Mistral

from .config import LLM_MODEL, with_retry

# Minimum gap between API calls (seconds). Our Mistral rate limit is
# 6 req/s; we use half that to leave headroom.
_MIN_CALL_GAP = 1.0 / 3
_last_call_time = 0.0


def _throttle() -> None:
    """Wait if needed to respect API rate limits."""
    global _last_call_time
    now = time.monotonic()
    elapsed = now - _last_call_time
    if elapsed < _MIN_CALL_GAP:
        time.sleep(_MIN_CALL_GAP - elapsed)
    _last_call_time = time.monotonic()


@with_retry(max_retries=3)
def call_llm(
    client: Mistral,
    prompt: str,
    *,
    system: str | None = None,
    json_mode: bool = False,
) -> str:
    """Call the LLM. Always uses temperature=0 for consistency."""
    _throttle()

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    kwargs = {"model": LLM_MODEL, "messages": messages, "temperature": 0}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    response = client.chat.complete(**kwargs)
    return response.choices[0].message.content.strip()


def _strip_code_blocks(text: str) -> str:
    """Extract content from markdown code blocks (```json ... ```)."""
    lines = text.split("\n")
    json_lines = []
    in_block = False
    for line in lines:
        if line.strip().startswith("```"):
            in_block = not in_block
            continue
        if in_block:
            json_lines.append(line)
    return "\n".join(json_lines) if json_lines else text


def _extract_json_object(text: str) -> str | None:
    """Find the outermost {...} in text, handling nested braces."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _fix_common_json_errors(text: str) -> str:
    """Fix common LLM JSON mistakes: trailing commas, control characters."""
    # Remove trailing commas before } or ]
    text = re.sub(r",\s*([}\]])", r"\1", text)
    # Strip control characters (except newlines/tabs) that break json.loads
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return text


def parse_json_response(result: str) -> dict:
    """Parse JSON from an LLM response.

    Tries several strategies in order:
    1. Direct parse (fast path for well-formed responses)
    2. Strip markdown code blocks (```json ... ```)
    3. Extract outermost {...} from surrounding prose
    4. Fix common JSON errors (trailing commas, control chars)

    Raises json.JSONDecodeError with a diagnostic message if all fail.
    """
    # 1. Try direct parse
    try:
        return json.loads(result)
    except json.JSONDecodeError:
        pass

    # 2. Try stripping code blocks
    stripped = _strip_code_blocks(result)
    if stripped != result:
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            try:
                return json.loads(_fix_common_json_errors(stripped))
            except json.JSONDecodeError:
                pass

    # 3. Try extracting outermost JSON object from prose
    extracted = _extract_json_object(result)
    if extracted:
        try:
            return json.loads(extracted)
        except json.JSONDecodeError:
            # 4. Try fixing common errors on the extracted object
            fixed = _fix_common_json_errors(extracted)
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                pass

    # Nothing worked — raise with a preview of what we got
    preview = result[:200] + ("..." if len(result) > 200 else "")
    raise json.JSONDecodeError(
        f"Could not extract valid JSON from LLM response: {preview!r}",
        result,
        0,
    )
