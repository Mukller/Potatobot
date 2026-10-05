"""Formatting utilities."""

from app.config import get_settings


def format_kg(kg: float) -> str:
    settings = get_settings()
    return f"{kg:.{settings.dig_precision}f}"


def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} сек"
    minutes = seconds // 60
    secs = seconds % 60
    if minutes < 60:
        return f"{minutes} мин {secs} сек"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours} ч {mins} мин"

def escape_html(value) -> str:
    """Escape text for Telegram parse_mode=HTML.

    Handles & first, otherwise the ampersands we introduce would be escaped
    again. Unknown/None values become an empty string so f-strings stay safe.
    """
    if value is None:
        return ""
    s = str(value)
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))


def truncate(text: str, limit: int) -> str:
    """Clamp a rendered line so a long leaderboard cannot exceed Telegram limits."""
    if text is None:
        return ""
    s = str(text)
    return s if len(s) <= limit else s[:max(0, limit - 1)] + "\u2026"
