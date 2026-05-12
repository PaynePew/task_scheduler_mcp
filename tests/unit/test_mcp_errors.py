"""Unit tests for app/mcp/errors.py — domain exception → error envelope mapping."""

from app.domain.jobs import UnknownActionError, UnsupportedScheduleTypeError
from app.mcp.errors import map_domain_error


def test_unknown_action_maps_to_unknown_action_code():
    result = map_domain_error(UnknownActionError("bad_action"))
    assert result["ok"] is False
    assert result["error"]["code"] == "UNKNOWN_ACTION"
    assert result["error"]["field"] == "action"
    assert "expected" in result["error"]


def test_unsupported_schedule_type_maps_to_user_input():
    result = map_domain_error(UnsupportedScheduleTypeError("recurring"))
    assert result["ok"] is False
    assert result["error"]["code"] == "USER_INPUT"
    assert result["error"]["field"] == "schedule_type"
    assert result["error"]["expected"] == "immediate"


def test_generic_exception_maps_to_internal():
    result = map_domain_error(RuntimeError("something broke"))
    assert result["ok"] is False
    assert result["error"]["code"] == "INTERNAL"
    assert "field" not in result["error"]


def test_value_error_maps_to_user_input():
    result = map_domain_error(ValueError("bad input"))
    assert result["ok"] is False
    assert result["error"]["code"] == "USER_INPUT"


def test_error_message_is_non_empty():
    result = map_domain_error(UnknownActionError("x"))
    assert len(result["error"]["message"]) > 0
