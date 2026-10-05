"""Middleware package: error_handling, logging, rate_limit."""

import time
from collections import defaultdict
from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery, ErrorEvent
from aiogram.exceptions import TelegramAPIError
from app.config import get_settings
from app.exceptions import CooldownError, ValidationError, DatabaseError, UserNotFoundError
from app.utils.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()


# ===== ErrorHandlingMiddleware =====

async def _tell(event, text: str) -> None:
    """Answer the user regardless of the event type.

    A CallbackQuery MUST be answered or Telegram keeps the button spinning and
    the user never sees anything.
    """
    if isinstance(event, CallbackQuery):
        try:
            await event.answer(text, show_alert=True)
        except TelegramAPIError:
            pass
    elif isinstance(event, Message):
        try:
            await event.answer(text)
        except TelegramAPIError:
            pass


class ErrorHandlingMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        try:
            return await handler(event, data)
        except CooldownError as e:
            mins = e.remaining // 60
            secs = e.remaining % 60
            if mins > 0:
                await _tell(event, f"⏳ Подожди {mins} мин {secs} сек до следующей копки")
            else:
                await _tell(event, f"⏳ Подожди {secs} сек до следующей копки")
        except ValidationError as e:
            await _tell(event, f"❌ {e.message}")
        except UserNotFoundError:
            await _tell(event, "❌ Пользователь не найден. Попробуй /start")
        except DatabaseError:
            logger.error("database_error", exc_info=True)
            await _tell(event, "🔧 Ошибка базы данных. Попробуй позже.")
        except TelegramAPIError as e:
            logger.warning("telegram_api_error", error=str(e))
        except Exception:
            logger.error("unhandled_error", exc_info=True)
            await _tell(event, "💥 Произошлась ошибка. Админы уже уведомлены.")


# ===== BanMiddleware =====

# Shared so the admin panel can invalidate an entry the moment it bans or
# unbans someone: a cached True would keep silencing a user for up to TTL
# after the admin deliberately let them back in.
_BAN_CACHE: dict[int, tuple[float, bool]] = {}
BAN_CACHE_TTL = 30.0


def forget_ban(tg_id: int) -> None:
    _BAN_CACHE.pop(tg_id, None)


class BanMiddleware(BaseMiddleware):
    """Silently drop everything from banned accounts.

    A ban that answers "you are banned" teaches the person exactly which rule
    got them, so the message is swallowed instead. Only the log knows.
    """

    def __init__(self):
        self._cache = _BAN_CACHE

    async def _is_banned(self, db, user_id: int) -> bool:
        now = time.time()
        hit = self._cache.get(user_id)
        if hit and now - hit[0] < BAN_CACHE_TTL:
            return hit[1]
        try:
            row = await db.fetchval(
                "SELECT is_banned FROM users WHERE tg_id = $1", user_id)
        except Exception:
            # A database hiccup must not lock everyone out.
            return False
        banned = bool(row)
        self._cache[user_id] = (now, banned)
        if len(self._cache) > 5000:
            self._cache.clear()
        return banned

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        bot = data.get("bot")
        if user is None or bot is None or getattr(bot, "db", None) is None:
            return await handler(event, data)

        # The admin must always be able to unban, including himself.
        if user.id in settings.admin_ids:
            return await handler(event, data)

        if await self._is_banned(bot.db, user.id):
            logger.info("banned_user_message_dropped", user_id=user.id)
            return None
        return await handler(event, data)


# ===== LoggingMiddleware =====

class LoggingMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Message, data):
        logger.info(
            "command_received",
            user_id=event.from_user.id,
            username=event.from_user.username,
            command=event.text,
        )
        return await handler(event, data)


# ===== RateLimitMiddleware =====

class RateLimitMiddleware(BaseMiddleware):
    def __init__(self):
        self.max_requests = settings.rate_limit_requests
        self.window = settings.rate_limit_window
        self.requests: dict[int, list[float]] = defaultdict(list)

    def _sweep(self, now: float) -> None:
        """Drop entries nobody is using, so the dict cannot grow forever."""
        stale = [uid for uid, ts in self.requests.items()
                 if not ts or now - ts[-1] > self.window]
        for uid in stale:
            self.requests.pop(uid, None)

    async def __call__(self, handler, event: Message, data):
        user_id = event.from_user.id
        now = time.time()

        # Clean old requests (for this user)
        self.requests[user_id] = [
            ts for ts in self.requests[user_id] if now - ts < self.window
        ]
        # Periodic sweep of every other user, amortised
        self._sweep_every = getattr(self, '_sweep_every', 0) + 1
        if self._sweep_every >= 100:
            self._sweep_every = 0
            self._sweep(now)

        if len(self.requests[user_id]) >= self.max_requests:
            logger.warning("rate_limit_exceeded", user_id=user_id)
            await event.answer("⏳ Слишком много запросов. Подожди немного.")
            return

        self.requests[user_id].append(now)
        return await handler(event, data)


__all__ = [
    "ErrorHandlingMiddleware",
    "LoggingMiddleware",
    "RateLimitMiddleware",
    "BanMiddleware",
    "forget_ban",
]