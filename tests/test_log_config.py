import json
import logging

from log_config import JsonFormatter, setup_logging


def make_record(msg="hello", extra=None, exc_info=None):
    logger = logging.getLogger("fmt_test")
    return logger.makeRecord(
        "fmt_test", logging.INFO, "file.py", 1, msg, None, exc_info, extra=extra
    )


def test_formats_basic_fields_as_json():
    out = JsonFormatter().format(make_record())
    data = json.loads(out)

    assert data["message"] == "hello"
    assert data["level"] == "INFO"
    assert data["logger"] == "fmt_test"
    assert "timestamp" in data


def test_extra_fields_are_included():
    record = make_record(extra={"repo": "owner/repo", "pr_number": 7})
    data = json.loads(JsonFormatter().format(record))

    assert data["repo"] == "owner/repo"
    assert data["pr_number"] == 7


def test_exception_is_included():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        record = make_record(msg="failed", exc_info=sys.exc_info())

    data = json.loads(JsonFormatter().format(record))

    assert "ValueError: boom" in data["exception"]


def test_setup_logging_does_not_duplicate_handlers():
    first = setup_logging("dedupe_handler_test")
    second = setup_logging("dedupe_handler_test")

    assert first is second
    assert len(first.handlers) == 1
