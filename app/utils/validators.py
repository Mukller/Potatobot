"""Validators for user input."""

import re
from app.config import get_settings


def validate_username(username: str | None) -> str | None:
    """Validate and sanitize Telegram username."""
    if username is None:
        return None
    # Telegram usernames: 5-32 chars, alphanumeric + underscore
    username = username.strip()
    if not username:
        return None
    if len(username) > 32:
        username = username[:32]
    # Remove @ if present
    if username.startswith("@"):
        username = username[1:]
    # Validate characters
    if not re.match(r"^[A-Za-z0-9_]+$", username):
        return None
    return username


def validate_dig_amount(kg: float) -> float:
    """Validate dig amount is within bounds."""
    settings = get_settings()
    if kg < settings.dig_min_kg:
        return settings.dig_min_kg
    if kg > settings.dig_max_kg:
        return settings.dig_max_kg
    return round(kg, settings.dig_precision)