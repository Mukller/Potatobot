"""Web routes: health and metrics. Nothing else.

The web admin panel and the /api/v1 endpoints were removed on purpose:

* the admin panel needed a session cookie and a signed URL, which is a browser
  session to get wrong;
* require_auth() trusted the X-Telegram-Init-Data header WITHOUT verifying its
  signature, so anybody could forge a Telegram ID header and read or write any
  user's data (POST /api/v1/dig even dug on someone else's behalf).

The bot drives Telegram directly, so the HTTP surface now exists only to answer
the Docker healthcheck and Prometheus.
"""

from aiohttp import web
from prometheus_client import generate_latest

from app.database import Database
from app.utils.logging import get_logger

logger = get_logger(__name__)


async def health_check(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    if await db.health_check():
        return web.json_response({"status": "healthy", "database": "connected"})
    return web.json_response(
        {"status": "unhealthy", "database": "disconnected"},
        status=503,
    )


async def readiness_check(request: web.Request) -> web.Response:
    return web.json_response({"status": "ready"})


async def metrics_handler(request: web.Request) -> web.Response:
    return web.Response(body=generate_latest(), content_type="text/plain")


def setup_routes(app: web.Application) -> None:
    app.router.add_get("/health", health_check)
    app.router.add_get("/ready", readiness_check)
    app.router.add_get("/metrics", metrics_handler)


__all__ = [
    "health_check",
    "readiness_check",
    "metrics_handler",
    "setup_routes",
]