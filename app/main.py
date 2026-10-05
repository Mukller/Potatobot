"""Main entry point for Potatobot."""

import asyncio
import signal
import sys
import sentry_sdk
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sentry_sdk.integrations.logging import LoggingIntegration

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from app.config import get_settings
from app.database import Database, init_database
from app.services import (
    HeartbeatService,
    get_dig_service,
    get_cleanup_service,
    get_daily_bonus_service,
    get_achievement_service,
    get_metrics_service,
    get_clan_service,
    get_ml_service,
    get_chaos_service,
)
from app.handlers import commands_router, features_router, admin_router
from app.middleware import (
    BanMiddleware,
    RateLimitMiddleware,
    ErrorHandlingMiddleware,
    LoggingMiddleware,
)
from app.web import create_web_app, run_web_server
from app.utils.logging import setup_logging, get_logger

logger = get_logger(__name__)


def init_sentry(settings) -> None:
    """Initialize Sentry SDK."""
    if not settings.sentry_dsn:
        logger.info("sentry_disabled")
        return

    sentry_logging = LoggingIntegration(
        level=None,  # Capture all levels
        event_level=None,  # Send all events
    )

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        profiles_sample_rate=settings.sentry_profiles_sample_rate,
        integrations=[
            AsyncioIntegration(),
            sentry_logging,
        ],
        environment="production",
        release="potatobot@2.1.0",
    )
    logger.info("sentry_initialized")


async def wait_for_database(db, dsn: str, attempts: int = 30, delay: float = 2.0) -> None:
    """Postgres can still be starting up right after a reboot; retry, then create schema."""
    last = None
    for i in range(attempts):
        try:
            await db.connect()
            logger.info("database_pool_ready", attempt=i + 1)
            await init_database(dsn)
            return
        except Exception as e:
            last = e
            logger.warning("database_not_ready", attempt=i + 1, error=str(e))
            await asyncio.sleep(delay)
    raise RuntimeError(f"database unavailable after {attempts} attempts: {last}")


async def main():
    settings = get_settings()

    # Setup logging
    setup_logging(settings.log_level, settings.log_format)
    logger.info("bot_starting", version="2.1.0")

    # Initialize Sentry
    init_sentry(settings)

    # Initialize database
    db = Database(
        settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
    )
    await wait_for_database(db, settings.database_url)
    logger.info("database_connected")

    # Initialize services
    dig_service = get_dig_service()
    dig_service.db = db

    cleanup_service = get_cleanup_service(db)
    await cleanup_service.start()

    metrics_service = get_metrics_service(db)
    await metrics_service.start()

    heartbeat_service = HeartbeatService()
    await heartbeat_service.start()

    clan_service = get_clan_service(db)

    ml_service = get_ml_service(db)

    daily_bonus_service = get_daily_bonus_service()
    daily_bonus_service.db = db

    achievement_service = get_achievement_service(db)

    chaos_service = get_chaos_service()

    # Initialize bot
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    bot.db = db
    bot.dig_service = dig_service
    bot.daily_bonus_service = daily_bonus_service
    bot.achievement_service = achievement_service
    bot.clan_service = clan_service
    bot.ml_service = ml_service
    bot.settings = settings
    bot.admin_ids = settings.admin_ids

    dp = Dispatcher()

    # Register middlewares (order matters!)
    # Ban first: a banned account must not reach the rate limiter's counters
    # or any other handler.
    dp.message.middleware(BanMiddleware())
    dp.message.middleware(ErrorHandlingMiddleware())
    dp.message.middleware(LoggingMiddleware())
    dp.message.middleware(RateLimitMiddleware())

    # Register handlers
    dp.include_router(commands_router)
    dp.include_router(features_router)
    dp.include_router(admin_router)

    # Set bot commands
    await bot.set_my_commands([
        BotCommand(command="start", description="🏁 Начать / перезапустить"),
        BotCommand(command="dig", description="🥔 Выкопать картошку"),
        BotCommand(command="my_stats", description="📊 Моя статистика"),
        BotCommand(command="my_history", description="📜 История копок"),
        BotCommand(command="top_day", description="🏆 Топ за сутки"),
        BotCommand(command="top_all", description="🏆 Общий топ"),
        BotCommand(command="daily", description="🎁 Ежедневный бонус"),
        BotCommand(command="achievements", description="🏅 Достижения"),
        BotCommand(command="clan", description="🏷 Мой клан"),
        BotCommand(command="clan_create", description="🏷 Создать клан"),
        BotCommand(command="clan_invite", description="📨 Пригласить в клан"),
        BotCommand(command="clan_invites", description="📨 Входящие приглашения"),
        BotCommand(command="clan_top", description="🏆 Топ кланов"),
        BotCommand(command="clan_transfer", description="👑 Передать владение кланом"),
        BotCommand(command="help", description="❓ Помощь"),
        BotCommand(command="recommend", description="🤖 Рекомендации"),
    ])

    # Start web server
    web_app = await create_web_app(db, settings, bot)
    web_runner = await run_web_server(web_app, settings.web_host, settings.web_port)
    logger.info("web_server_started", host=settings.web_host, port=settings.web_port)

    # Graceful shutdown
    shutdown_event = asyncio.Event()

    def signal_handler():
        logger.info("shutdown_signal_received")
        shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            asyncio.get_event_loop().add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    # Start polling
    logger.info("bot_polling_started")
    polling_task = asyncio.create_task(dp.start_polling(bot))

    try:
        await shutdown_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("bot_shutting_down")
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass

        await cleanup_service.stop()
        await metrics_service.stop()
        await heartbeat_service.stop()
        await web_runner.cleanup()
        await bot.session.close()
        await db.close()
        logger.info("bot_stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass