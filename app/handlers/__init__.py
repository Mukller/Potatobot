"""Handlers package."""

from app.handlers.commands import router as commands_router
from app.handlers.features import router as features_router
from app.handlers.admin_panel import router as admin_router, admin_keyboard

__all__ = [
    "commands_router", "features_router", "admin_router",
    "admin_keyboard",
]