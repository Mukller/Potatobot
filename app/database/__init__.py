"""Database connection and models."""

from __future__ import annotations

import asyncpg
import time
from app.exceptions import DatabaseError
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Optional
from app.config import get_settings
from sqlalchemy import BigInteger, Float, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class UserModel(Base):
    __tablename__ = "users"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    total_kg: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    last_dig_time: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)


class DigHistoryModel(Base):
    __tablename__ = "dig_history"

    dig_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False)
    kg: Mapped[float] = mapped_column(Float, nullable=False)
    timestamp: Mapped[float] = mapped_column(Float, nullable=False)


Index("idx_dig_history_user_timestamp", DigHistoryModel.user_id, DigHistoryModel.timestamp.desc())
Index("idx_dig_history_timestamp", DigHistoryModel.timestamp)


@dataclass
class User:
    user_id: int
    tg_id: int
    username: Optional[str]
    total_kg: float
    last_dig_time: Optional[float]
    created_at: float
    digs_count: int = 0


@dataclass
class DigRecord:
    dig_id: int
    user_id: int
    kg: float
    timestamp: float


@dataclass
class LeaderboardEntry:
    rank: int
    username: str
    total_kg: float
    digs_count: int


DB_ERRORS = (
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
    asyncpg.InvalidAuthorizationSpecificationError,
    OSError,
    ConnectionError,
)


class Database:
    def __init__(
        self,
        dsn: str,
        min_size: int = 10,
        max_size: int = 20,
    ):
        self.dsn = dsn
        self.min_size = min_size
        self.max_size = max_size
        self._pool: Optional[asyncpg.Pool] = None

    @property
    def pool(self) -> Optional[asyncpg.Pool]:
        """The live pool, or None before connect()."""
        return self._pool

    # One-off reads and writes. Using these instead of `async with
    # self.acquire()` keeps single statements short; loops still take a
    # connection once via acquire().
    async def fetchval(self, query: str, *args):
        async with self.acquire() as conn:
            return await conn.fetchval(query, *args)

    async def fetchrow(self, query: str, *args):
        async with self.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetch(self, query: str, *args):
        async with self.acquire() as conn:
            return await conn.fetch(query, *args)

    async def execute(self, query: str, *args):
        async with self.acquire() as conn:
            return await conn.execute(query, *args)

    async def connect(self) -> None:
        try:
            self._pool = await asyncpg.create_pool(
                self.dsn,
                min_size=self.min_size,
                max_size=self.max_size,
                command_timeout=30,
                server_settings={
                    "application_name": "potatobot",
                    "timezone": "UTC",
                },
            )
        except DB_ERRORS as e:
            raise DatabaseError(f"connect: {type(e).__name__}: {e}") from e

    @asynccontextmanager
    async def acquire(self):
        if self._pool is None:
            raise DatabaseError("Database not connected")
        try:
            async with self._pool.acquire() as conn:
                yield conn
        except DatabaseError:
            raise
        except DB_ERRORS as e:
            raise DatabaseError(f"acquire: {type(e).__name__}: {e}") from e

    @asynccontextmanager
    async def transaction(self):
        """All-or-nothing unit of work. Rolls back on any exception."""
        if self._pool is None:
            raise DatabaseError("Database not connected")
        try:
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    yield conn
        except DatabaseError:
            raise
        except DB_ERRORS as e:
            raise DatabaseError(f"transaction: {type(e).__name__}: {e}") from e

    async def health_check(self) -> bool:
        try:
            async with self.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    # User queries
    async def get_or_create_user(self, tg_id: int, username: Optional[str]) -> User:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO users (tg_id, username, total_kg, last_dig_time, created_at)
                VALUES ($1, $2, 0.0, NULL, EXTRACT(EPOCH FROM NOW()))
                ON CONFLICT (tg_id) DO UPDATE SET username = EXCLUDED.username
                RETURNING user_id, tg_id, username, total_kg, last_dig_time, created_at, digs_count
                """,
                tg_id,
                username,
            )
            return User(
                user_id=row["user_id"],
                tg_id=row["tg_id"],
                username=row["username"],
                total_kg=row["total_kg"],
                last_dig_time=row["last_dig_time"],
                created_at=row["created_at"],
                digs_count=row.get("digs_count", 0),
            )

    async def get_user_by_tg_id(self, tg_id: int) -> Optional[User]:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT user_id, tg_id, username, total_kg, last_dig_time, created_at, digs_count FROM users WHERE tg_id = $1",
                tg_id,
            )
            if row:
                return User(
                    user_id=row["user_id"],
                    tg_id=row["tg_id"],
                    username=row["username"],
                    total_kg=row["total_kg"],
                    last_dig_time=row["last_dig_time"],
                    created_at=row["created_at"],
                    digs_count=row["digs_count"],
                )
            return None

    async def dig_with_cooldown(self, user_id: int, kg: float, timestamp: float,
                                cooldown: int) -> tuple[Optional[User], Optional[int], int]:
        """Atomically: enforce the cooldown AND record the dig.

        Returns (user, dig_id, seconds_remaining).
        user is None when the cooldown rejected the write; seconds_remaining
        is then > 0.
        """
        async with self.transaction() as conn:
            row = await conn.fetchrow(
                """
                UPDATE users
                SET total_kg = total_kg + $2::double precision,
                    last_dig_time = $3::double precision,
                    digs_count = digs_count + 1
                WHERE user_id = $1
                  AND (last_dig_time IS NULL
                       OR last_dig_time <= $3::double precision - $4::double precision)
                RETURNING user_id, tg_id, username, total_kg, last_dig_time,
                          created_at, digs_count
                """,
                user_id, kg, timestamp, cooldown,
            )
            if row is None:
                remaining = await conn.fetchval(
                    """
                    SELECT $3::double precision - last_dig_time
                    FROM users WHERE user_id = $1
                    """,
                    user_id, kg, timestamp,
                )
                return None, None, max(1, int(remaining or cooldown))

            dig_id = await conn.fetchval(
                """
                INSERT INTO dig_history (user_id, kg, timestamp)
                VALUES ($1, $2::double precision, $3::double precision)
                RETURNING dig_id
                """,
                user_id, kg, timestamp,
            )
            user = User(
                user_id=row["user_id"],
                tg_id=row["tg_id"],
                username=row["username"],
                total_kg=row["total_kg"],
                last_dig_time=row["last_dig_time"],
                created_at=row["created_at"],
                digs_count=row["digs_count"],
            )
            return user, dig_id, 0

    async def bonus_with_cooldown(self, user_id: int, kg: float, timestamp: float,
                                  cooldown: int) -> tuple[Optional[User], Optional[int], int]:
        """Same guarantee for the daily-bonus write path."""
        return await self.dig_with_cooldown(user_id, kg, timestamp, cooldown)

    async def set_banned(self, tg_id: int, banned: bool) -> bool | None:
        """Ban or unban an account. Returns the new state, or None when the
        account has never used the bot and therefore has no row."""
        async with self.acquire() as conn:
            return await conn.fetchval(
                "UPDATE users SET is_banned = $2 WHERE tg_id = $1 RETURNING is_banned",
                tg_id, banned,
            )

    async def is_banned(self, tg_id: int) -> bool:
        async with self.acquire() as conn:
            row = await conn.fetchval(
                "SELECT is_banned FROM users WHERE tg_id = $1", tg_id)
        return bool(row)

    async def admin_summary(self) -> dict:
        """Everything the admin panel shows on the stats screen."""
        day_ago = time.time() - 86400
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    (SELECT count(*) FROM users)                       AS users_total,
                    (SELECT count(*) FROM users WHERE is_banned)       AS banned_total,
                    (SELECT count(*) FROM users WHERE created_at >= $1) AS users_today,
                    (SELECT count(*) FROM dig_history WHERE timestamp >= $1) AS digs_today,
                    (SELECT COALESCE(sum(kg), 0) FROM dig_history
                        WHERE timestamp >= $1)                       AS kg_today,
                    (SELECT count(DISTINCT user_id) FROM dig_history
                        WHERE timestamp >= $1)                       AS diggers_today,
                    (SELECT count(*) FROM clans)                      AS clans_total,
                    (SELECT COALESCE(sum(total_kg), 0) FROM users)    AS kg_total
                """,
                day_ago,
            )
        return {k: row[k] for k in row.keys()}

    async def admin_recent_users(self, limit: int = 10, offset: int = 0) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT user_id, tg_id, username, total_kg, digs_count,
                       is_banned, created_at
                FROM users ORDER BY user_id DESC LIMIT $1 OFFSET $2
                """,
                limit, offset,
            )
        return [dict(r) for r in rows]

    async def admin_user_card(self, tg_id: int) -> dict | None:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT u.user_id, u.tg_id, u.username, u.total_kg, u.digs_count,
                       u.is_banned, u.created_at,
                       (SELECT count(*) FROM dig_history d
                         WHERE d.user_id = u.user_id)             AS digs_kept,
                       (SELECT COALESCE(sum(kg), 0) FROM dig_history d
                         WHERE d.user_id = u.user_id)             AS kg_kept
                FROM users u WHERE u.tg_id = $1
                """,
                tg_id,
            )
        return dict(row) if row else None

    async def update_user_dig(self, user_id: int, kg: float, timestamp: float) -> User:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE users
                SET total_kg = total_kg + $2,
                    last_dig_time = $3,
                    digs_count = digs_count + 1
                WHERE user_id = $1
                RETURNING user_id, tg_id, username, total_kg, last_dig_time, created_at, digs_count
                """,
                user_id,
                kg,
                timestamp,
            )
            return User(
                user_id=row["user_id"],
                tg_id=row["tg_id"],
                username=row["username"],
                total_kg=row["total_kg"],
                last_dig_time=row["last_dig_time"],
                created_at=row["created_at"],
                digs_count=row.get("digs_count", 0),
            )

    # Dig history queries
    async def add_dig_record(self, user_id: int, kg: float, timestamp: float) -> int:
        async with self.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO dig_history (user_id, kg, timestamp)
                VALUES ($1, $2::double precision, $3::double precision)
                RETURNING dig_id
                """,
                user_id,
                kg,
                timestamp,
            )

    async def get_user_history(self, user_id: int, limit: int = 10) -> list[DigRecord]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT dig_id, user_id, kg, timestamp
                FROM dig_history
                WHERE user_id = $1
                ORDER BY timestamp DESC
                LIMIT $2
                """,
                user_id,
                limit,
            )
            return [
                DigRecord(
                    dig_id=row["dig_id"],
                    user_id=row["user_id"],
                    kg=row["kg"],
                    timestamp=row["timestamp"],
                )
                for row in rows
            ]

    # Leaderboard queries
    async def get_top_day(self, limit: int = 10) -> list[LeaderboardEntry]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT u.username, COALESCE(SUM(dh.kg), 0) as total_kg, COUNT(dh.dig_id) as digs_count
                FROM users u
                LEFT JOIN dig_history dh ON u.user_id = dh.user_id
                    AND dh.timestamp > EXTRACT(EPOCH FROM NOW()) - 86400
                GROUP BY u.user_id, u.username
                HAVING COALESCE(SUM(dh.kg), 0) > 0
                ORDER BY total_kg DESC
                LIMIT $1
                """,
                limit,
            )
            return [
                LeaderboardEntry(
                    rank=i + 1,
                    username=row["username"] or f"User_{row['user_id']}",
                    total_kg=row["total_kg"],
                    digs_count=row["digs_count"],
                )
                for i, row in enumerate(rows)
            ]

    async def get_top_all(self, limit: int = 10) -> list[LeaderboardEntry]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT u.username, u.total_kg, COUNT(dh.dig_id) as digs_count
                FROM users u
                LEFT JOIN dig_history dh ON u.user_id = dh.user_id
                GROUP BY u.user_id, u.username, u.total_kg
                HAVING u.total_kg > 0
                ORDER BY u.total_kg DESC
                LIMIT $1
                """,
                limit,
            )
            return [
                LeaderboardEntry(
                    rank=i + 1,
                    username=row["username"] or f"User_{row['user_id']}",
                    total_kg=row["total_kg"],
                    digs_count=row["digs_count"],
                )
                for i, row in enumerate(rows)
            ]

    # Maintenance
    async def cleanup_old_history(self, retention_seconds: int) -> int:
        async with self.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM dig_history
                WHERE timestamp < EXTRACT(EPOCH FROM NOW()) - $1
                """,
                retention_seconds,
            )
            return int(result.split()[-1]) if result else 0

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id       SERIAL PRIMARY KEY,
        tg_id         BIGINT UNIQUE NOT NULL,
        username      TEXT,
        total_kg      REAL NOT NULL DEFAULT 0.0,
        last_dig_time DOUBLE PRECISION,
        digs_count    INTEGER NOT NULL DEFAULT 0,
        is_banned     BOOLEAN NOT NULL DEFAULT false,
        created_at    DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dig_history (
        dig_id    SERIAL PRIMARY KEY,
        user_id   INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        kg        REAL NOT NULL,
        timestamp DOUBLE PRECISION NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dig_history_user_timestamp ON dig_history (user_id, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_dig_history_timestamp ON dig_history (timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_dig_history_user_id ON dig_history (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_users_tg_id ON users (tg_id)",
    """
    CREATE TABLE IF NOT EXISTS clans (
        clan_id        SERIAL PRIMARY KEY,
        name           TEXT NOT NULL,
        tag            TEXT UNIQUE NOT NULL,
        description    TEXT DEFAULT '',
        owner_id       INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        owner_username TEXT,
        total_kg       REAL NOT NULL DEFAULT 0.0,
        created_at     DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS clan_members (
        clan_id   INTEGER NOT NULL REFERENCES clans(clan_id) ON DELETE CASCADE,
        user_id   INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        role      TEXT NOT NULL DEFAULT 'member',
        joined_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        PRIMARY KEY (clan_id, user_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_clan_members_user ON clan_members (user_id)",
    """
    CREATE TABLE IF NOT EXISTS clan_invites (
        invite_id       SERIAL PRIMARY KEY,
        clan_id         INTEGER NOT NULL REFERENCES clans(clan_id) ON DELETE CASCADE,
        inviter_id      INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        invited_user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        created_at      DOUBLE PRECISION NOT NULL DEFAULT 0.0
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_clan_invites_unique ON clan_invites (clan_id, invited_user_id)",
    "CREATE INDEX IF NOT EXISTS idx_clan_invites_invited ON clan_invites (invited_user_id)",
    """
    CREATE TABLE IF NOT EXISTS user_achievements (
        user_id        INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
        achievement_id TEXT NOT NULL,
        unlocked_at    DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        PRIMARY KEY (user_id, achievement_id)
    )
    """,
]

# Dead tables from the removed web panel / web API. sessions and counters were
# only ever written by the browser admin panel and the (signature-less) HTTP
# API; with both gone nothing reads or writes them.
DROP_DEAD_TABLES = [
    "DROP TABLE IF EXISTS sessions",
    "DROP TABLE IF EXISTS counters",
]

# Additive migrations for columns added after the first release. All of them
# are idempotent, so init_database() stays the single source of truth.
ADDITIVE_COLUMNS = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN NOT NULL DEFAULT false",
]

# Timestamp columns must be DOUBLE PRECISION: REAL is float4, whose
# granularity at unix time ~1.79e9 is 128s, which rounds last_dig_time into
# the future and silently disables the dig cooldown.
WIDEN_TIMESTAMP_COLUMNS = [
    "ALTER TABLE users ALTER COLUMN last_dig_time TYPE DOUBLE PRECISION",
    "ALTER TABLE users ALTER COLUMN created_at TYPE DOUBLE PRECISION",
    "ALTER TABLE dig_history ALTER COLUMN timestamp TYPE DOUBLE PRECISION",
    "ALTER TABLE clans ALTER COLUMN created_at TYPE DOUBLE PRECISION",
    "ALTER TABLE clan_members ALTER COLUMN joined_at TYPE DOUBLE PRECISION",
    "ALTER TABLE clan_invites ALTER COLUMN created_at TYPE DOUBLE PRECISION",
    "ALTER TABLE user_achievements ALTER COLUMN unlocked_at TYPE DOUBLE PRECISION",
]


async def init_database(dsn: str) -> None:
    """Initialize database schema (idempotent)."""
    conn = await asyncpg.connect(dsn)
    try:
        for stmt in SCHEMA_STATEMENTS:
            await conn.execute(stmt)
        # Widen any pre-existing REAL timestamp columns to float8.
        for stmt in WIDEN_TIMESTAMP_COLUMNS:
            try:
                await conn.execute(stmt)
            except Exception:
                pass  # column/table may not exist yet
        for stmt in ADDITIVE_COLUMNS:
            await conn.execute(stmt)
        for stmt in DROP_DEAD_TABLES:
            await conn.execute(stmt)
    finally:
        await conn.close()
