"""Tests for UI/runtime helpers: duration formatting, error classification, run log."""

import pytest

from translation_lib.formatting import fmt_duration
from translation_lib.llm_utils import describe_api_error
from translation_lib.runlog import RunLog


@pytest.mark.parametrize("seconds,expected", [
    (0, "0s"),
    (5, "5s"),
    (59, "59s"),
    (60, "1m00s"),
    (65, "1m05s"),
    (3600, "1h00m"),
    (3661, "1h01m"),
])
def test_fmt_duration(seconds, expected):
    assert fmt_duration(seconds) == expected


class _ApiError(Exception):
    def __init__(self, status_code=None, message=""):
        super().__init__(message)
        self.status_code = status_code


def test_describe_auth_error():
    msg = describe_api_error(_ApiError(status_code=401))
    assert msg and "MISTRAL_API_KEY" in msg


def test_describe_forbidden_error():
    assert describe_api_error(_ApiError(status_code=403)) is not None


def test_describe_rate_limit():
    msg = describe_api_error(_ApiError(status_code=429))
    assert msg and "rate-limit" in msg.lower()


def test_describe_server_error():
    msg = describe_api_error(_ApiError(status_code=503))
    assert msg and "server error" in msg.lower()


def test_describe_connection_error():
    msg = describe_api_error(ConnectionError("Connection refused"))
    assert msg and "internet connection" in msg.lower()


def test_describe_unknown_error_returns_none():
    assert describe_api_error(ValueError("something odd")) is None


def test_runlog_mirrors_and_log_only(tmp_path):
    log_path = tmp_path / "run.log"
    runlog = RunLog(log_path)
    try:
        print("visible line")
        runlog.log_only("log-only line\n")
    finally:
        runlog.close()

    content = log_path.read_text(encoding="utf-8")
    assert "visible line" in content
    assert "log-only line" in content
