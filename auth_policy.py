"""Authentication policy helpers shared by setup and web signup paths."""

MIN_DASHBOARD_PASSWORD_LENGTH = 10


def validate_dashboard_password(password: str) -> tuple[bool, str]:
    """Validate dashboard password and return (is_valid, error_message)."""
    if not isinstance(password, str):
        return False, "Password must be a string."

    if len(password) < MIN_DASHBOARD_PASSWORD_LENGTH:
        return (
            False,
            f"Password must be at least {MIN_DASHBOARD_PASSWORD_LENGTH} characters.",
        )

    if not password.strip():
        return False, "Password cannot be empty."

    return True, ""
