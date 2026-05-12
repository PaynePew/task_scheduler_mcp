"""Unit tests for app/mcp/envelope.py — success and error shape."""

from app.mcp.envelope import error, success


def test_success_wraps_data():
    result = success({"job_id": 42, "status": "scheduled"})
    assert result == {"ok": True, "data": {"job_id": 42, "status": "scheduled"}}


def test_success_ok_is_true():
    assert success({})["ok"] is True


def test_error_full_shape():
    result = error("USER_INPUT", "Bad action", field="action", expected="echo")
    assert result == {
        "ok": False,
        "error": {
            "code": "USER_INPUT",
            "message": "Bad action",
            "field": "action",
            "expected": "echo",
        },
    }


def test_error_without_optional_fields():
    result = error("INTERNAL", "Server error")
    assert result == {"ok": False, "error": {"code": "INTERNAL", "message": "Server error"}}
    assert "field" not in result["error"]
    assert "expected" not in result["error"]


def test_error_ok_is_false():
    assert error("INTERNAL", "x")["ok"] is False


def test_error_with_field_only():
    result = error("NOT_FOUND", "Not found", field="job_id")
    assert result["error"]["field"] == "job_id"
    assert "expected" not in result["error"]
