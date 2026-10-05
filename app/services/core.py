"""Core services: UserService, DigService, DailyBonusService, CleanupService, RedisService."""

import asyncio
import time
import random
from typing import Optional
from dataclasses import dataclass
from app.config import get_settings
from app.database import Database, User, DigRecord
from app.utils.logging import get_logger
from app.utils.validators import validate_dig_amount
from app.utils.formatting import format_kg
from app.exceptions import CooldownError, ValidationError, DatabaseError, UserNotFoundError

logger = get_logger(__name__)
settings = get_settings()


# ===== Global service instances =====

_dig_service = None
_daily_bonus_service = None
_cleanup_service = None


# ===== DigService =====

class DigService:
    def __init__(self, db: Database):
        self.db = db

    async def perform_dig(self, tg_id: int, username: Optional[str]) -> dict:
        user = await self.db.get_or_create_user(tg_id, username)
        now = time.time()

        # Cooldown is enforced atomically in the UPDATE below; this early check
        # only produces a nicer message without a round trip.
        if user.last_dig_time and 0 <= (now - user.last_dig_time) < settings.dig_cooldown_seconds:
            remaining = int(settings.dig_cooldown_seconds - (now - user.last_dig_time))
            raise CooldownError(remaining)

        # Anti-cheat: minimum interval
        if settings.anticheat_enabled and user.last_dig_time and 0 <= (now - user.last_dig_time) < settings.anticheat_min_dig_interval:
            raise ValidationError("Слишком быстро! Подожди немного.")

        # Generate weight
        kg = round(random.uniform(settings.dig_min_kg, settings.dig_max_kg), settings.dig_precision)
        kg = validate_dig_amount(kg)

        # Anti-cheat: max kg per hour
        if settings.anticheat_enabled:
            recent_digs = await self.get_recent_digs(user.user_id, 3600)
            total_kg_hour = sum(d.kg for d in recent_digs) + kg
            if total_kg_hour > settings.anticheat_max_kg_per_hour:
                logger.warning("anticheat_triggered", user_id=tg_id, total_kg_hour=total_kg_hour)
                raise ValidationError("Подозрительная активность. Попробуй позже.")

        # Atomic: cooldown guard + weight update + history row in one
        # transaction. A double tap can no longer slip past the cooldown.
        user, dig_id, remaining = await self.db.dig_with_cooldown(
            user.user_id, kg, now, settings.dig_cooldown_seconds)
        if user is None:
            raise CooldownError(remaining or settings.dig_cooldown_seconds)

        # Check achievements
        from app.services import get_achievement_service
        achievement_service = get_achievement_service()
        new_achievements = await achievement_service.check_achievements(user.user_id, kg, user.total_kg)

        # Update metrics
        from app.services import get_metrics_service
        metrics_service = get_metrics_service(self.db)
        await metrics_service.record_dig(user.user_id, kg, "success")

        # Update daily bonus streak activity
        from app.services import get_daily_bonus_service
        daily_bonus_service = get_daily_bonus_service()
        daily_bonus_service.db = self.db
        await daily_bonus_service.record_activity(user.user_id)

        return {
            "kg": kg,
            "total_kg": user.total_kg,
            "dig_id": dig_id,
            "achievements": new_achievements,
        }

    async def get_user_history(self, user_id: int, limit: int = 10) -> list[DigRecord]:
        return await self.db.get_user_history(user_id, limit)

    async def get_recent_digs(self, user_id: int, seconds: int) -> list[DigRecord]:
        """Get digs within last N seconds."""
        history = await self.db.get_user_history(user_id, 100)
        cutoff = time.time() - seconds
        return [d for d in history if d.timestamp >= cutoff]


# ===== DailyBonusService =====

@dataclass
class DailyBonusStatus:
    can_claim: bool
    streak: int
    wait_seconds: int
    next_bonus_kg: float


class DailyBonusService:
    def __init__(self):
        self.db: Optional[Database] = None
        self._cache = {}

    def get_cached_status(self, user_id: int) -> Optional[DailyBonusStatus]:
        key = f"daily_status_{user_id}"
        if key in self._cache:
            expires, status = self._cache[key]
            if time.time() < expires:
                return status
            del self._cache[key]
        return None

    def set_cached_status(self, user_id: int, status: DailyBonusStatus, ttl: int = 60):
        key = f"daily_status_{user_id}"
        self._cache[key] = (time.time() + ttl, status)
        self._evict_expired(time.time())

    def _evict_expired(self, now: float) -> None:
        """Bounded cache: drop expired entries for everyone, not just on access."""
        if len(self._cache) < 512:
            return
        for k in [k for k, (exp, _st) in self._cache.items() if now > exp]:
            self._cache.pop(k, None)
        # hard cap in case entries are all fresh
        while len(self._cache) > 2048:
            self._cache.pop(next(iter(self._cache)), None)

    async def get_status(self, tg_id: int) -> dict:
        cached = self.get_cached_status(tg_id)
        if cached:
            return {
                "can_claim": cached.can_claim,
                "streak": cached.streak,
                "wait_seconds": cached.wait_seconds,
                "next_bonus_kg": cached.next_bonus_kg,
            }

        user = await self.db.get_user_by_tg_id(tg_id)
        if not user:
            raise UserNotFoundError()

        now = time.time()
        last_claim = user.last_dig_time or 0

        # Check if daily bonus was claimed today (reset at midnight UTC)
        today_start = int(now // 86400) * 86400
        can_claim = last_claim < today_start

        streak = 0
        if user.last_dig_time:
            last_day_start = int(user.last_dig_time // 86400) * 86400
            days_diff = max(0, (today_start - last_day_start) // 86400)
            if days_diff <= 1:
                streak = days_diff

        wait_seconds = 0
        if not can_claim:
            wait_seconds = int(today_start + 86400 - now)

        next_bonus_kg = settings.daily_bonus_base_kg
        if streak > 0:
            next_bonus_kg += streak * settings.daily_bonus_streak_multiplier * settings.daily_bonus_base_kg
        next_bonus_kg = min(next_bonus_kg, settings.daily_bonus_base_kg * (1 + settings.daily_bonus_max_streak * settings.daily_bonus_streak_multiplier))

        status = DailyBonusStatus(can_claim, streak, wait_seconds, next_bonus_kg)
        self.set_cached_status(tg_id, status)

        return {
            "can_claim": can_claim,
            "streak": streak,
            "wait_seconds": wait_seconds,
            "next_bonus_kg": next_bonus_kg,
        }

    async def claim(self, tg_id: int) -> dict:
        user = await self.db.get_user_by_tg_id(tg_id)
        if not user:
            raise UserNotFoundError()

        status = await self.get_status(tg_id)
        if not status["can_claim"]:
            raise ValidationError("Бонус уже получен сегодня")

        kg = status["next_bonus_kg"]
        now = time.time()

        # Atomic, same guarantees as a dig: cooldown guard + row + history.
        user, dig_id, remaining = await self.db.bonus_with_cooldown(
            user.user_id, kg, now, 86400)
        if user is None:
            raise ValidationError("Бонус уже получен сегодня")

        # Check achievements
        from app.services import get_achievement_service
        achievement_service = get_achievement_service()
        new_achievements = await achievement_service.check_achievements(user.user_id, kg, user.total_kg)

        # Update streak (simplified)
        new_streak = status["streak"] + 1 if status["streak"] > 0 else 1

        # Invalidate cache
        self._cache.pop(f"daily_status_{tg_id}", None)

        from app.services import get_metrics_service
        metrics_service = get_metrics_service(self.db)
        await metrics_service.record_daily_bonus(tg_id, new_streak, "success")

        return {
            "kg": kg,
            "total_kg": user.total_kg,
            "streak": new_streak,
            "dig_id": dig_id,
            "achievements": new_achievements,
        }

    async def record_activity(self, tg_id: int):
        """Record user activity for streak tracking."""
        user = await self.db.get_user_by_tg_id(tg_id)
        if user:
            now = time.time()
            await self.db.update_user_dig(user.user_id, 0, now)  # Just update last_dig_time


# ===== CleanupService =====

class CleanupService:
    def __init__(self, db: Database):
        self.db = db
        self._task = None

    async def start(self):
        self._task = asyncio.create_task(self._cleanup_loop())
        logger.info("cleanup_service_started")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("cleanup_service_stopped")

    async def _cleanup_loop(self):
        while True:
            try:
                await asyncio.sleep(settings.cleanup_interval_seconds)
                await self.cleanup()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("cleanup_error", error=str(e))

    async def cleanup(self):
        deleted = await self.db.cleanup_old_history(settings.history_retention_hours * 3600)
        if deleted > 0:
            logger.info("history_cleaned", deleted=deleted)
        # The sessions/counters tables this used to prune belonged to the web
        # panel and its signature-less HTTP API. Both are gone, so dig_history
        # is the only table that grows, and it is trimmed above.



# ===== UserService =====

class UserService:
    def __init__(self, db: Database):
        self.db = db

    async def get_or_create_user(self, tg_id: int, username: Optional[str]) -> User:
        return await self.db.get_or_create_user(tg_id, username)

    async def get_user(self, tg_id: int) -> Optional[User]:
        return await self.db.get_user_by_tg_id(tg_id)

    async def update_username(self, tg_id: int, username: Optional[str]) -> Optional[User]:
        user = await self.db.get_user_by_tg_id(tg_id)
        if not user:
            return None
        # Username is updated on get_or_create_user
        return await self.db.get_or_create_user(tg_id, username)


# ===== Factory functions =====

def get_dig_service() -> DigService:
    global _dig_service
    if _dig_service is None:
        # Will be initialized with db in main.py
        _dig_service = DigService(None)
    return _dig_service


def get_daily_bonus_service() -> DailyBonusService:
    global _daily_bonus_service
    if _daily_bonus_service is None:
        _daily_bonus_service = DailyBonusService()
    return _daily_bonus_service


def get_cleanup_service(db: Database) -> CleanupService:
    global _cleanup_service
    if _cleanup_service is None:
        _cleanup_service = CleanupService(db)
    return _cleanup_service



# ===== Export for main.py =====

__all__ = [
    "DigService",
    "DailyBonusService",
    "CleanupService",
    "UserService",
    "get_dig_service",
    "get_daily_bonus_service",
    "get_cleanup_service",
]