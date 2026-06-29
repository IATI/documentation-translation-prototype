"""Tests for the JSON-parsing robustness in llm_utils.

The LLM is the only source of translations, so parse_json_response is the
gatekeeper that prevents malformed model output from reaching the PO files.
"""

import json

import pytest

from translation_lib.llm_utils import parse_json_response


def test_parses_clean_json():
    assert parse_json_response('{"translation": "Bonjour"}') == {"translation": "Bonjour"}


def test_parses_fenced_code_block():
    raw = '```json\n{"translation": "Hola"}\n```'
    assert parse_json_response(raw) == {"translation": "Hola"}


def test_extracts_object_from_prose():
    raw = 'Sure, here you go: {"translation": "Hola"} — hope that helps!'
    assert parse_json_response(raw) == {"translation": "Hola"}


def test_fixes_trailing_comma():
    assert parse_json_response('{"translation": "Hola",}') == {"translation": "Hola"}


def test_strips_control_characters():
    raw = '{"translation": "line\x07break"}'
    assert parse_json_response(raw) == {"translation": "linebreak"}


def test_raises_on_unparseable():
    with pytest.raises(json.JSONDecodeError):
        parse_json_response("this is not json at all")
