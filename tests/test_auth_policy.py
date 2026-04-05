import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auth_policy import MIN_DASHBOARD_PASSWORD_LENGTH, validate_dashboard_password


def test_short_password_is_rejected():
    candidate = "x" * (MIN_DASHBOARD_PASSWORD_LENGTH - 1)
    ok, error = validate_dashboard_password(candidate)

    assert ok is False
    assert str(MIN_DASHBOARD_PASSWORD_LENGTH) in error


def test_minimum_length_password_is_accepted():
    candidate = "x" * MIN_DASHBOARD_PASSWORD_LENGTH
    ok, error = validate_dashboard_password(candidate)

    assert ok is True
    assert error == ""


def test_whitespace_only_password_is_rejected():
    candidate = " " * MIN_DASHBOARD_PASSWORD_LENGTH
    ok, error = validate_dashboard_password(candidate)

    assert ok is False
    assert "cannot be empty" in error
