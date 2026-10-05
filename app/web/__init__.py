"""Web server setup - health and metrics only."""

from aiohttp import web

from app.web.routes import setup_routes


async def create_web_app(db, settings, bot=None) -> web.Application:
    app = web.Application()
    app["db"] = db
    app["settings"] = settings
    if bot:
        app["bot"] = bot
    setup_routes(app)
    return app


async def run_web_server(app: web.Application, host: str, port: int):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner


__all__ = ["create_web_app", "run_web_server"]