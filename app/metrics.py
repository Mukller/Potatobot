"""Prometheus metric objects in one place.

They used to live in web/routes.py, which made them unreachable from the
services that actually produce the events - so every counter stayed at zero.
Both the HTTP exporter and the services now import from here.
"""

from prometheus_client import Counter, Gauge, Histogram

# Dig
DIG_COMMANDS = Counter(
    "potatobot_dig_commands_total",
    "Total dig commands",
    ["status"],
)
DIG_KG = Histogram(
    "potatobot_dig_kg",
    "Potato weight per dig",
    buckets=[1, 2, 3, 4, 5, 6, 7],
)
DIG_KG_BY_USER = Histogram(
    "potatobot_dig_weight_by_user_kg",
    "Weight distribution per user",
    ["user_id"],
    buckets=[1, 2, 3, 4, 5, 6, 7],
)

# Users
ACTIVE_USERS_DAILY = Gauge("potatobot_active_users_daily", "DAU")
ACTIVE_USERS_WEEKLY = Gauge("potatobot_active_users_weekly", "WAU")
ACTIVE_USERS_MONTHLY = Gauge("potatobot_active_users_monthly", "MAU")
RETENTION_D1 = Gauge("potatobot_retention_day1", "Day 1 retention")
RETENTION_D7 = Gauge("potatobot_retention_day7", "Day 7 retention")
RETENTION_D30 = Gauge("potatobot_retention_day30", "Day 30 retention")

# Cache
CACHE_HITS = Counter(
    "potatobot_cache_hits_total",
    "Cache hits",
    ["cache_name"],
)
CACHE_MISSES = Counter(
    "potatobot_cache_misses_total",
    "Cache misses",
    ["cache_name"],
)

# DB
DB_QUERY_DURATION = Histogram(
    "potatobot_db_query_duration_seconds",
    "DB query duration",
)
DB_POOL_USAGE = Gauge("potatobot_db_pool_usage", "Pool usage", ["state"])
DB_QUERY_ERRORS = Counter("potatobot_db_query_errors_total", "DB errors", ["query"])

# Errors
ERRORS_TOTAL = Counter("potatobot_errors_total", "Errors", ["type", "handler"])

# Daily bonus
DAILY_BONUS_CLAIMS = Counter(
    "potatobot_daily_bonus_claims_total", "Daily bonus claims", ["status"]
)
DAILY_BONUS_STREAK = Gauge(
    "potatobot_daily_bonus_streak", "Current streak", ["user_id"]
)

# Achievements
ACHIEVEMENTS_UNLOCKED = Counter(
    "potatobot_achievements_unlocked_total",
    "Achievements unlocked",
    ["achievement_id"],
)

__all__ = [
    "DIG_COMMANDS", "DIG_KG", "DIG_KG_BY_USER",
    "ACTIVE_USERS_DAILY", "ACTIVE_USERS_WEEKLY", "ACTIVE_USERS_MONTHLY",
    "RETENTION_D1", "RETENTION_D7", "RETENTION_D30",
    "CACHE_HITS", "CACHE_MISSES",
    "DB_QUERY_DURATION", "DB_POOL_USAGE", "DB_QUERY_ERRORS",
    "ERRORS_TOTAL",
    "DAILY_BONUS_CLAIMS", "DAILY_BONUS_STREAK",
    "ACHIEVEMENTS_UNLOCKED",
]
