"""Heartbeat task: prove the bot's event loop is alive.

The cron healthcheck used to grep the container log for "Run polling", which
aiogram logs exactly once at startup - so five minutes later the grep found
nothing and the check restarted a perfectly healthy bot every eight minutes.

A heartbeat written by a task on the same event loop as dp.start_polling()
answers the question that was actually being asked: is the process still
running its loop, or has it wedged?
"""

import asyncio
import os
import time

from app.config import get_settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Path inside the container. /tmp is writable by the appuser.
HEARTBEAT_FILE = "/tmp/potatobot.heartbeat"
# How often to write it. The healthcheck allows three missed writes.
INTERVAL_SECONDS = 30


def heartbeat_age(path: str = HEARTBEAT_FILE) -> float:
    """Seconds since the last beat, or inf when there is no beat at all."""
    try:
        return max(0.0, time.time() - os.stat(path).st_mtime)
    except OSError:
        return float("inf")


def _beat(path: str) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(f"{time.time():.0f}\n")
    os.replace(tmp, path)          # atomic: a reader never sees a half file


class HeartbeatService:
    def __init__(self, path: str = HEARTBEAT_FILE,
                 interval: int = INTERVAL_SECONDS):
        self.path = path
        self.interval = interval
        self._task: asyncio.Task | None = None

    async def _loop(self) -> None:
        while True:
            try:
                _beat(self.path)
            except OSError as e:
                # A heartbeat we cannot write must not kill the bot; the
                # healthcheck will notice the missing file and alert.
                logger.warning("heartbeat_write_failed", error=str(e))
            await asyncio.sleep(self.interval)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            logger.info("heartbeat_started", path=self.path,
                        interval=self.interval)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            try:
                os.remove(self.path)
            except OSError:
                pass
            logger.info("heartbeat_stopped")


__all__ = ["HeartbeatService", "HEARTBEAT_FILE", "INTERVAL_SECONDS",
           "heartbeat_age"]