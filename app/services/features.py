"""Feature services: AchievementService, ClanService, MetricsService, MLRecommendationService."""

import time
import random
import asyncio
from typing import Optional
from dataclasses import dataclass
from app.config import get_settings
from app.database import Database, User, LeaderboardEntry
from app.utils.logging import get_logger
from app.utils.cache import TTLCache
from app.exceptions import ValidationError, UserNotFoundError, CooldownError

logger = get_logger(__name__)
settings = get_settings()


# ===== Global service instances =====

_achievement_service = None
_clan_service = None
_metrics_service = None
_ml_service = None


# ===== AchievementService =====

@dataclass
class Achievement:
    id: str
    name: str
    description: str
    icon: str
    condition: str  # Description of unlock condition


ACHIEVEMENTS = {
    "first_dig": Achievement("first_dig", "Первая копка", "Выкопал первую картошку", "🥔", "1 копка"),
    "hundred_kg": Achievement("hundred_kg", "Сотня", "Набрал 100 кг картошки", "💯", "100 кг"),
    "thousand_kg": Achievement("thousand_kg", "Тысяча", "Набрал 1000 кг картошки", "🎖", "1000 кг"),
    "ten_k_kg": Achievement("ten_k_kg", "Десять тонн", "Набрал 10 000 кг картошки", "🏆", "10 000 кг"),
    "streak_7": Achievement("streak_7", "Неделя удачи", "Получил бонус 7 дней подряд", "🔥", "Стрик 7 дней"),
    "streak_30": Achievement("streak_30", "Месяц удачи", "Получил бонус 30 дней подряд", "🌟", "Стрик 30 дней"),
    "hundred_digs": Achievement("hundred_digs", "Труженик", "Совершил 100 копок", "⛏", "100 копок"),
    "thousand_digs": Achievement("thousand_digs", "Мастер копания", "Совершил 1000 копок", "🏅", "1000 копок"),
    "max_dig": Achievement("max_dig", "Рекордсмен", "Выкопал картошку макс. веса", "👑", "7.0 кг за раз"),
    "early_bird": Achievement("early_bird", "Ранняя пташка", "Копал в 6 утра", "🌅", "Копка 06:00-07:00"),
    "night_owl": Achievement("night_owl", "Новая сова", "Копал в 3 ночи", "🦉", "Копка 03:00-04:00"),
    "clan_creator": Achievement("clan_creator", "Основатель", "Создал клан", "🏷", "Создать клан"),
    "clan_leader": Achievement("clan_leader", "Вожак", "Становится лидером клана", "👑", "Владеть кланом"),
    "top_day": Achievement("top_day", "Король дня", "1-е место в топе за день", "🥇", "Топ-1 за день"),
    "top_week": Achievement("top_week", "Король недели", "1-е место в топе за неделю", "🏆", "Топ-1 за неделю"),
    "lucky_dig": Achievement("lucky_dig", "Везунчик", "Выкопал 6.9-7.0 кг", "🍀", "Вес 6.9-7.0 кг"),
    "social": Achievement("social", "Общительный", "Пригласил 5 друзей в клан", "🤝", "5 приглашений"),
    "collector": Achievement("collector", "Коллекционер", "Разблокировал 10 достижений", "📚", "10 достижений"),
    "marathon": Achievement("marathon", "Марафонец", "Копал 24 часа подряд", "🏃", "Активность каждые 1ч 24ч"),
    "weight_watcher": Achievement("weight_watcher", "Контролёр веса", "Проверил статистику 50 раз", "📊", "50 просмотров статистики"),
    "web_user": Achievement("web_user", "Веб-мастер", "Воспользовался веб-приложением", "🌐", "Открыть веб-приложение"),
    "recommender": Achievement("recommender", "Советчик", "Получил 10 рекомендаций", "🤖", "10 рекомендаций"),
}


class AchievementService:
    """Achievements are evaluated on each dig and persisted, once each."""

    def __init__(self):
        self.db: Optional[Database] = None

    def get_achievement(self, ach_id: str) -> Optional[Achievement]:
        return ACHIEVEMENTS.get(ach_id)

    def get_all_achievements(self) -> list[Achievement]:
        return list(ACHIEVEMENTS.values())

    async def get_user_achievements(self, user_id: int) -> set[str]:
        """Read unlocked achievements from the database (no cache: cheap, and
        correctness beats a 5-minute stale list)."""
        if self.db is None:
            return set()
        try:
            async with self.db.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT achievement_id FROM user_achievements WHERE user_id = $1",
                    user_id,
                )
        except Exception as e:
            logger.warning("achievements_read_failed", user_id=user_id, error=str(e))
            return set()
        return {r["achievement_id"] for r in rows}

    async def unlock(self, user_id: int, ach_id: str) -> bool:
        """Unlock if not already. Returns True only when it was newly added."""
        if self.db is None:
            return False
        try:
            async with self.db.acquire() as conn:
                res = await conn.execute(
                    """
                    INSERT INTO user_achievements (user_id, achievement_id, unlocked_at)
                    VALUES ($1, $2, $3)
                    ON CONFLICT DO NOTHING
                    """,
                    user_id, ach_id, time.time(),
                )
        except Exception as e:
            # cosmetic feature: never let it break the dig that triggered it
            logger.warning("achievement_unlock_failed", user_id=user_id,
                           achievement=ach_id, error=str(e))
            return False
        return res.endswith("1")  # "INSERT 0 1" vs "INSERT 0 0"

    async def check_achievements(self, user_id: int, last_dig_kg: float,
                                 total_kg: float) -> list[dict]:
        """Evaluate every condition and persist the ones that just fired."""
        if self.db is None:
            return []

        digs_count = 0
        try:
            async with self.db.acquire() as conn:
                digs_count = await conn.fetchval(
                    "SELECT digs_count FROM users WHERE user_id = $1", user_id) or 0
        except Exception:
            digs_count = 0
        total = total_kg or 0.0

        already = await self.get_user_achievements(user_id)
        candidates: list[str] = []

        if digs_count >= 1:
            candidates.append("first_dig")
        if total >= 100:
            candidates.append("hundred_kg")
        if total >= 1000:
            candidates.append("thousand_kg")
        if total >= 10000:
            candidates.append("ten_k_kg")
        if digs_count >= 100:
            candidates.append("hundred_digs")
        if digs_count >= 1000:
            candidates.append("thousand_digs")
        if last_dig_kg >= settings.dig_max_kg * 0.99:
            candidates.append("max_dig")
        if last_dig_kg >= 6.9:
            candidates.append("lucky_dig")

        hour = time.localtime().tm_hour
        if hour == 6:
            candidates.append("early_bird")
        elif hour == 3:
            candidates.append("night_owl")

        new: list[dict] = []
        for ach_id in candidates:
            ach = ACHIEVEMENTS.get(ach_id)
            if ach is None or ach_id in already:
                continue
            if await self.unlock(user_id, ach_id):
                new.append({"id": ach.id, "name": ach.name,
                            "description": ach.description, "icon": ach.icon})

        if new:
            try:
                from app.metrics import ACHIEVEMENTS_UNLOCKED
                for item in new:
                    ACHIEVEMENTS_UNLOCKED.labels(
                        achievement_id=item["id"]).inc()
            except Exception as e:
                logger.warning("metric_achievement_failed", error=str(e))
            logger.info("achievements_unlocked", user_id=user_id,
                        ids=[n["id"] for n in new])
        return new


# ===== ClanService =====

@dataclass
class Clan:
    clan_id: int
    name: str
    tag: str
    description: str
    owner_id: int
    owner_username: str
    total_kg: float
    created_at: float


@dataclass
class ClanMember:
    user_id: int
    tg_id: int
    username: str
    role: str  # leader, officer, member
    total_kg: float
    joined_at: float


@dataclass
class ClanInvite:
    invite_id: int
    clan_id: int
    inviter_id: int
    inviter_username: str
    invited_user_id: int
    created_at: float


class ClanService:
    def __init__(self, db: Database):
        self.db = db

    async def create_clan(self, owner_id: int, name: str, tag: str, description: str, owner_username: str) -> Clan:
        async with self.db.acquire() as conn:
            # Check if tag exists
            existing = await conn.fetchval("SELECT clan_id FROM clans WHERE tag = $1", tag)
            if existing:
                raise ValidationError("Такой тэг уже занят")

            clan_id = await conn.fetchval("""
                INSERT INTO clans (name, tag, description, owner_id, owner_username, total_kg, created_at)
                VALUES ($1, $2, $3, $4, $5, 0.0, $6)
                RETURNING clan_id
            """, name, tag, description, owner_id, owner_username, time.time())

            # Add owner as member
            await conn.execute("""
                INSERT INTO clan_members (clan_id, user_id, role, joined_at)
                VALUES ($1, $2, 'leader', $3)
            """, clan_id, owner_id, time.time())

            return Clan(
                clan_id=clan_id,
                name=name,
                tag=tag,
                description=description,
                owner_id=owner_id,
                owner_username=owner_username,
                total_kg=0.0,
                created_at=time.time(),
            )

    async def get_clan(self, clan_id: int) -> Optional[Clan]:
        async with self.db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM clans WHERE clan_id = $1", clan_id
            )
            if row:
                return Clan(**dict(row))
            return None

    async def get_user_clan(self, tg_id: int) -> Optional[Clan]:
        user = await self.db.get_user_by_tg_id(tg_id)
        if not user:
            return None

        async with self.db.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT c.* FROM clans c
                JOIN clan_members cm ON c.clan_id = cm.clan_id
                WHERE cm.user_id = $1
            """, user.user_id)
            if row:
                return Clan(**dict(row))
            return None

    async def get_clan_members(self, clan_id: int) -> list[ClanMember]:
        async with self.db.acquire() as conn:
            rows = await conn.fetch("""
                SELECT u.user_id, u.tg_id, u.username, u.total_kg, cm.role, cm.joined_at
                FROM clan_members cm
                JOIN users u ON cm.user_id = u.user_id
                WHERE cm.clan_id = $1
                ORDER BY cm.role DESC, u.total_kg DESC
            """, clan_id)
            return [ClanMember(**dict(r)) for r in rows]

    async def get_member(self, clan_id: int, tg_id: int) -> Optional[ClanMember]:
        user = await self.db.get_user_by_tg_id(tg_id)
        if not user:
            return None

        async with self.db.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT u.user_id, u.tg_id, u.username, u.total_kg, cm.role, cm.joined_at
                FROM clan_members cm
                JOIN users u ON cm.user_id = u.user_id
                WHERE cm.clan_id = $1 AND cm.user_id = $2
            """, clan_id, user.user_id)
            if row:
                return ClanMember(**dict(row))
            return None

    async def invite_user(self, clan_id: int, user_id: int, inviter_id: int):
        async with self.db.acquire() as conn:
            # Check if already member
            existing = await conn.fetchval(
                "SELECT 1 FROM clan_members WHERE clan_id = $1 AND user_id = $2",
                clan_id, user_id
            )
            if existing:
                raise ValidationError("Уже участник клана")

            # Check if already invited
            existing = await conn.fetchval(
                "SELECT 1 FROM clan_invites WHERE clan_id = $1 AND invited_user_id = $2",
                clan_id, user_id
            )
            if existing:
                raise ValidationError("Уже приглашен")

            await conn.execute("""
                INSERT INTO clan_invites (clan_id, inviter_id, invited_user_id, created_at)
                VALUES ($1, $2, $3, $4)
            """, clan_id, inviter_id, user_id, time.time())

    async def get_user_invites(self, tg_id: int) -> list[ClanInvite]:
        user = await self.db.get_user_by_tg_id(tg_id)
        if not user:
            return []

        async with self.db.acquire() as conn:
            rows = await conn.fetch("""
                SELECT ci.*, u.username as inviter_username
                FROM clan_invites ci
                JOIN users u ON ci.inviter_id = u.user_id
                WHERE ci.invited_user_id = $1
            """, user.user_id)
            return [ClanInvite(**dict(r)) for r in rows]

    async def accept_invite(self, invite_id: int, tg_id: int) -> Clan:
        user = await self.db.get_user_by_tg_id(tg_id)
        if not user:
            raise UserNotFoundError()

        async with self.db.acquire() as conn:
            invite = await conn.fetchrow("SELECT * FROM clan_invites WHERE invite_id = $1", invite_id)
            if not invite or invite["invited_user_id"] != user.user_id:
                raise ValidationError("Приглашение не найдено")

            clan_id = invite["clan_id"]
            clan = await self.get_clan(clan_id)

            # Add as member
            await conn.execute("""
                INSERT INTO clan_members (clan_id, user_id, role, joined_at)
                VALUES ($1, $2, 'member', $3)
            """, clan_id, user.user_id, time.time())

            # Delete invite
            await conn.execute("DELETE FROM clan_invites WHERE invite_id = $1", invite_id)

            return clan

    async def reject_invite(self, invite_id: int):
        async with self.db.acquire() as conn:
            await conn.execute("DELETE FROM clan_invites WHERE invite_id = $1", invite_id)

    async def leave_clan(self, clan_id: int, tg_id: int):
        user = await self.db.get_user_by_tg_id(tg_id)
        if not user:
            return

        async with self.db.acquire() as conn:
            await conn.execute(
                "DELETE FROM clan_members WHERE clan_id = $1 AND user_id = $2",
                clan_id, user.user_id
            )

    async def transfer_ownership(self, clan_id: int, new_owner_id: int):
        async with self.db.acquire() as conn:
            # Update clan owner
            new_owner = await self.db.get_user_by_tg_id(new_owner_id)
            if not new_owner:
                raise UserNotFoundError()

            await conn.execute("""
                UPDATE clans SET owner_id = $1, owner_username = $2 WHERE clan_id = $3
            """, new_owner.user_id, new_owner.username or new_owner.first_name, clan_id)

            # Update roles
            await conn.execute("""
                UPDATE clan_members SET role = 'member' WHERE clan_id = $1 AND role = 'leader'
            """, clan_id)
            await conn.execute("""
                UPDATE clan_members SET role = 'leader' WHERE clan_id = $1 AND user_id = $2
            """, clan_id, new_owner.user_id)

    async def get_top_clans(self, limit: int = 10) -> list[Clan]:
        async with self.db.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM clans ORDER BY total_kg DESC LIMIT $1
            """, limit)
            return [Clan(**dict(r)) for r in rows]

    async def search_clans(self, query: str, limit: int = 10) -> list[Clan]:
        async with self.db.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM clans
                WHERE name ILIKE $1 OR tag ILIKE $1
                ORDER BY total_kg DESC
                LIMIT $2
            """, f"%{query}%", limit)
            return [Clan(**dict(r)) for r in rows]


# ===== MetricsService =====

class MetricsService:
    def __init__(self, db: Database):
        self.db = db
        self._task = None

    async def start(self):
        self._task = asyncio.create_task(self._metrics_loop())
        logger.info("metrics_service_started")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("metrics_service_stopped")

    async def _metrics_loop(self):
        while True:
            try:
                await asyncio.sleep(300)  # Every 5 minutes
                await self.update_retention_metrics()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("metrics_loop_error", error=str(e))

    async def record_dig(self, user_id: int, kg: float, status: str):
        """Record a dig attempt. Must never raise - metrics cannot break a dig."""
        try:
            from app.metrics import DIG_COMMANDS, DIG_KG, DIG_KG_BY_USER
            DIG_COMMANDS.labels(status=status).inc()
            if status == "success":
                DIG_KG.observe(kg)
                DIG_KG_BY_USER.labels(user_id=str(user_id)).observe(kg)
        except Exception as e:
            logger.warning("metric_record_failed", error=str(e))

    async def record_daily_bonus(self, user_id: int, streak: int, status: str):
        try:
            from app.metrics import DAILY_BONUS_CLAIMS, DAILY_BONUS_STREAK
            DAILY_BONUS_CLAIMS.labels(status=status).inc()
            DAILY_BONUS_STREAK.labels(user_id=str(user_id)).set(streak)
        except Exception as e:
            logger.warning("metric_record_failed", error=str(e))

    async def update_retention_metrics(self):
        """Update DAU/WAU/MAU and retention metrics."""
        async with self.db.acquire() as conn:
            # DAU - users active in last 24h
            dau = await conn.fetchval("""
                SELECT COUNT(DISTINCT user_id) FROM dig_history
                WHERE timestamp > EXTRACT(EPOCH FROM NOW()) - 86400
            """) or 0

            # WAU - users active in last 7 days
            wau = await conn.fetchval("""
                SELECT COUNT(DISTINCT user_id) FROM dig_history
                WHERE timestamp > EXTRACT(EPOCH FROM NOW()) - 604800
            """) or 0

            # MAU - users active in last 30 days
            mau = await conn.fetchval("""
                SELECT COUNT(DISTINCT user_id) FROM dig_history
                WHERE timestamp > EXTRACT(EPOCH FROM NOW()) - 2592000
            """) or 0

            try:
                from app.metrics import (
                    ACTIVE_USERS_DAILY, ACTIVE_USERS_WEEKLY, ACTIVE_USERS_MONTHLY,
                )
                ACTIVE_USERS_DAILY.set(dau)
                ACTIVE_USERS_WEEKLY.set(wau)
                ACTIVE_USERS_MONTHLY.set(mau)
            except Exception as e:
                logger.warning("metric_gauge_update_failed", error=str(e))
            logger.info("metrics_updated", dau=dau, wau=wau, mau=mau)


# ===== MLRecommendationService =====

@dataclass
class UserProfile:
    user_id: int
    total_kg: float
    digs_count: int
    avg_kg_per_dig: float
    streak: int
    achievements_count: int
    favorite_hour: Optional[int]
    favorite_day: Optional[int]
    clan_id: Optional[int]
    last_active: float


@dataclass
class Recommendation:
    title: str
    description: str
    priority: int  # 1-10


class MLRecommendationService:
    def __init__(self, db: Database):
        self.db = db
        self._cache = TTLCache(3600)  # 1 hour cache

    async def get_user_profile(self, user_id: int) -> Optional[UserProfile]:
        async with self.db.acquire() as conn:
            # Get user stats
            user = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", user_id)
            if not user:
                return None

            # Get dig stats
            dig_stats = await conn.fetchrow("""
                SELECT COUNT(*) as digs_count, AVG(kg) as avg_kg,
                       MAX(kg) as max_kg,
                       EXTRACT(HOUR FROM TO_TIMESTAMP(timestamp)) as hour,
                       EXTRACT(DOW FROM TO_TIMESTAMP(timestamp)) as dow
                FROM dig_history
                WHERE user_id = $1
            """, user_id)

            # Get favorite hour/day
            fav_hour = await conn.fetchval("""
                SELECT EXTRACT(HOUR FROM TO_TIMESTAMP(timestamp)) as hour
                FROM dig_history
                WHERE user_id = $1
                GROUP BY hour
                ORDER BY COUNT(*) DESC
                LIMIT 1
            """, user_id)

            fav_day = await conn.fetchval("""
                SELECT EXTRACT(DOW FROM TO_TIMESTAMP(timestamp)) as dow
                FROM dig_history
                WHERE user_id = $1
                GROUP BY dow
                ORDER BY COUNT(*) DESC
                LIMIT 1
            """, user_id)

            # Get clan
            clan_row = await conn.fetchrow("""
                SELECT clan_id FROM clan_members WHERE user_id = $1
            """, user_id)

            # Get achievements count
            # Simplified - in production you'd query achievements table

            return UserProfile(
                user_id=user_id,
                total_kg=user["total_kg"],
                digs_count=dig_stats["digs_count"] if dig_stats else 0,
                avg_kg_per_dig=dig_stats["avg_kg"] if dig_stats else 0,
                streak=0,  # Would need streak tracking
                achievements_count=0,
                favorite_hour=int(fav_hour) if fav_hour else None,
                favorite_day=int(fav_day) if fav_day else None,
                clan_id=clan_row["clan_id"] if clan_row else None,
                last_active=user["last_dig_time"] or 0,
            )

    def _generate_recommendations(self, profile: UserProfile) -> list[Recommendation]:
        recs = []

        if profile.digs_count < 10:
            recs.append(Recommendation(
                "Начни копать!",
                "Чем больше копок, тем быстрее растешь. Цель: 10 копок за день.",
                priority=9
            ))

        if profile.avg_kg_per_dig < 3.0 and profile.digs_count > 20:
            recs.append(Recommendation(
                "Попробуй копать в другое время",
                f"Твой средний вес {profile.avg_kg_per_dig:.1f} кг. Лучшие результаты часто ночью.",
                priority=7
            ))

        if profile.streak < 7:
            recs.append(Recommendation(
                "Поддерживай стрик бонусов",
                f"Текущий стрик: {profile.streak} дн. Каждый день увеличивает бонус!",
                priority=8
            ))

        if not profile.clan_id:
            recs.append(Recommendation(
                "Присоединись к клану",
                "Кланы дают бонусы к весу и доступ к клановым достижениям.",
                priority=6
            ))

        if profile.achievements_count < 5:
            recs.append(Recommendation(
                "Охотись за достижениями",
                f"У тебя {profile.achievements_count} достижений. Есть легкие: ранняя пташка, новая сова.",
                priority=5
            ))

        if profile.favorite_hour is not None:
            recs.append(Recommendation(
                f"Твое лучшее время: {profile.favorite_hour}:00",
                f"Статистика показывает, что в {profile.favorite_hour}:00 у тебя лучшие результаты.",
                priority=4
            ))

        return recs

    async def generate_recommendations(self, user_id: int) -> list[Recommendation]:
        profile = await self.get_user_profile(user_id)
        if not profile:
            return []
        return self._generate_recommendations(profile)

    async def get_cached_recommendations(self, user_id: int) -> list[Recommendation]:
        key = f"recommendations_{user_id}"
        cached = self._cache.get(key)
        if cached:
            return cached

        recs = await self.generate_recommendations(user_id)
        if recs:
            self._cache.set(key, recs)
        return recs

    async def invalidate_cache(self, user_id: int):
        self._cache.invalidate(f"recommendations_{user_id}")

    async def send_smart_notification(self, bot, user_id: int):
        """Send a smart notification based on user behavior."""
        profile = await self.get_user_profile(user_id)
        if not profile:
            return

        if time.time() - profile.last_active > 86400:
            try:
                await bot.send_message(
                    user_id,
                    "🥔 <b>Скучаем по тебе!</b>\n\n"
                    "Давно не копали? Заходи за ежедневным бонусом /daily "
                    "и продолжай набирать вес!",
                    parse_mode="HTML",
                )
            except Exception:
                pass


# ===== Factory functions =====

def get_achievement_service(db: Optional[Database] = None) -> AchievementService:
    global _achievement_service
    if _achievement_service is None:
        _achievement_service = AchievementService()
    if db is not None:
        _achievement_service.db = db
    return _achievement_service


def get_clan_service(db: Database) -> ClanService:
    global _clan_service
    if _clan_service is None:
        _clan_service = ClanService(db)
    return _clan_service


def get_metrics_service(db: Database) -> MetricsService:
    global _metrics_service
    if _metrics_service is None:
        _metrics_service = MetricsService(db)
    return _metrics_service


def get_ml_service(db: Database) -> MLRecommendationService:
    global _ml_service
    if _ml_service is None:
        _ml_service = MLRecommendationService(db)
    return _ml_service


__all__ = [
    "AchievementService",
    "get_achievement_service",
    "Achievement",
    "ClanService",
    "get_clan_service",
    "Clan",
    "ClanMember",
    "ClanInvite",
    "MetricsService",
    "get_metrics_service",
    "MLRecommendationService",
    "get_ml_service",
    "UserProfile",
    "Recommendation",
]