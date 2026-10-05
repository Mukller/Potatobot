"""Admin control panel. Telegram only - no web panel, no browser session.

Every admin action is a button in the chat. That is not a stylistic choice: the
web panel needed a session cookie plus a signed URL, and its HTTP API trusted an
unverified initData header, so anybody who knew the owner's Telegram ID could
read the whole database. None of that surface exists here - Telegram itself
delivers the Telegram ID, and the gate is settings.admin_ids.

Kept in its own module so it is easy to find and easy to delete.
"""

from __future__ import annotations

import time

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from app.config import get_settings
from app.middleware import forget_ban
from app.utils.logging import get_logger

router = Router(name="admin")
logger = get_logger(__name__)
settings = get_settings()

ADMIN_BUTTON = "⚙️ Админка"
ADMIN_ONLY = "🔒 Это только для администратора."
NOT_FOUND = "🔍 Юзер с таким ID не найден. Он должен хотя бы раз нажать /start."
LOOKUP_CANCELLED = "Ок, поиск отменён."

PAGE_SIZE = 8
_BOOT_TS = time.time()


class AdminLookup(StatesGroup):
    waiting_id = State()


def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


def admin_keyboard() -> ReplyKeyboardMarkup:
    """The button row, shown to the owner only."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=ADMIN_BUTTON)]],
        resize_keyboard=True,
        is_persistent=True,
    )


# ===== keyboards =====

def _menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats"),
            InlineKeyboardButton(text="👥 Юзеры", callback_data="adm:users:0"),
        ],
        [
            InlineKeyboardButton(text="🔎 Найти по ID", callback_data="adm:find"),
            InlineKeyboardButton(text="🩺 Здоровье", callback_data="adm:health"),
        ],
        [
            InlineKeyboardButton(text="🧪 Chaos", callback_data="adm:chaos"),
        ],
    ])


def _chaos_kb(status: dict) -> InlineKeyboardMarkup:
    on = bool(status.get("enabled"))
    rows = [[InlineKeyboardButton(
        text="❌ Выключить" if on else "✅ Включить",
        callback_data="adm:chaos:off" if on else "adm:chaos:on",
    )]]
    if on:
        rows.append([
            InlineKeyboardButton(text="🐢 Задержки", callback_data="adm:chaos:latency"),
            InlineKeyboardButton(text="💥 Ошибки", callback_data="adm:chaos:error"),
        ])
        rows.append([
            InlineKeyboardButton(text="⏱ Ресурсы", callback_data="adm:chaos:timeout"),
        ])
    rows.append([InlineKeyboardButton(text="◀️ Меню", callback_data="adm:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _pager_kb(page: int, has_more: bool) -> InlineKeyboardMarkup:
    row = []
    if page > 0:
        row.append(InlineKeyboardButton(text="⬅️", callback_data=f"adm:users:{page - 1}"))
    row.append(InlineKeyboardButton(text=f"стр. {page + 1}", callback_data="adm:noop"))
    if has_more:
        row.append(InlineKeyboardButton(text="➡️", callback_data=f"adm:users:{page + 1}"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


# ===== screens =====

async def _stats_screen(db) -> tuple[str, InlineKeyboardMarkup]:
    s = await db.admin_summary()
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"Пользователей: <b>{s['users_total']}</b> (+{s['users_today']} за сутки)\n"
        f"Копок за сутки: <b>{s['digs_today']}</b> "
        f"({float(s['kg_today']):.1f} кг, {s['diggers_today']} чел.)\n"
        f"Всего накоплено: <b>{float(s['kg_total']):.1f} кг</b>\n"
        f"Кланов: {s['clans_total']} · Забанено: {s['banned_total']}"
    )
    return text, _menu_kb()


async def _health_screen(db) -> tuple[str, InlineKeyboardMarkup]:
    db_ok = await db.health_check()
    pool = getattr(db, "pool", None)
    size = pool.get_size() if pool and hasattr(pool, "get_size") else "?"
    idle = pool.get_idle_size() if pool and hasattr(pool, "get_idle_size") else "?"
    uptime = int(time.time() - _BOOT_TS)
    text = (
        "🩺 <b>Здоровье</b>\n\n"
        f"Аптайм: {uptime // 3600} ч {(uptime % 3600) // 60} мин\n"
        f"База данных: <b>{'connected' if db_ok else 'НЕТ СВЯЗИ'}</b>\n"
        f"Пул соединений: {size} всего, {idle} свободно\n"
    )
    return text, _menu_kb()


def _users_screen(rows: list[dict], page: int) -> tuple[str, InlineKeyboardMarkup]:
    lines = []
    rows_kb = []
    for u in rows:
        name = f"@{u['username']}" if u.get("username") else "без ника"
        flag = " 🚫" if u.get("is_banned") else ""
        lines.append(
            f"<code>{u['tg_id']}</code> · {name}{flag}\n"
            f"    {float(u['total_kg']):.1f} кг · {u['digs_count']} копок"
        )
        rows_kb.append([InlineKeyboardButton(
            text=f"{name}{flag}",
            callback_data=f"adm:u:{page}:{u['tg_id']}",
        )])
    text = "👥 <b>Юзеры</b>\n\n" + ("\n".join(lines) or "Пока никого нет.")
    pager = _pager_kb(page, len(rows) == PAGE_SIZE).inline_keyboard[0]
    return text, InlineKeyboardMarkup(inline_keyboard=rows_kb + [pager])


def _card_screen(u: dict, page: int) -> tuple[str, InlineKeyboardMarkup]:
    name = f"@{u['username']}" if u.get("username") else "без ника"
    state = "забанен" if u["is_banned"] else "активен"
    text = (
        "👤 <b>Юзер</b>\n\n"
        f"Telegram ID: <code>{u['tg_id']}</code>\n"
        f"Имя: {name}\n"
        f"Всего: {float(u['total_kg']):.1f} кг · {u['digs_count']} копок\n"
        f"В истории (48 ч): {u['digs_kept']} копок, {float(u['kg_kept']):.1f} кг\n"
        f"Статус: <b>{state}</b>"
    )
    toggle = (
        InlineKeyboardButton(text="✅ Разбанить", callback_data=f"adm:unban:{u['tg_id']}")
        if u["is_banned"] else
        InlineKeyboardButton(text="🚫 Забанить", callback_data=f"adm:ban:{u['tg_id']}")
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [toggle],
        [InlineKeyboardButton(text="◀️ К списку", callback_data=f"adm:users:{page}")],
        [InlineKeyboardButton(text="◀️ Меню", callback_data="adm:menu")],
    ])
    return text, kb


def _chaos_screen(status: dict) -> tuple[str, InlineKeyboardMarkup]:
    on = bool(status.get("enabled"))
    text = (
        "🧪 <b>Chaos Engineering</b>\n\n"
        f"Состояние: <b>{'включён' if on else 'выключен'}</b>\n"
        f"Тип: {status.get('experiment_type') or '—'}\n"
        f"Интенсивность: {status.get('intensity', 0)}%"
    )
    return text, _chaos_kb(status)


# ===== guards =====

async def _deny(call: CallbackQuery) -> bool:
    """Answer 'no' to a non-admin. Returns True when access is granted."""
    if is_admin(call.from_user.id):
        return True
    await call.answer(ADMIN_ONLY, show_alert=True)
    return False


async def _paint(call: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# ===== entry point =====

@router.message(Command("admin"))
@router.message(F.text == ADMIN_BUTTON)
async def admin_open(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await message.answer(ADMIN_ONLY)
        return
    await state.clear()
    text, kb = await _stats_screen(message.bot.db)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "adm:noop")
async def admin_noop(call: CallbackQuery) -> None:
    await call.answer()


@router.callback_query(F.data == "adm:menu")
async def admin_menu(call: CallbackQuery) -> None:
    if not await _deny(call):
        return
    text, kb = await _stats_screen(call.bot.db)
    await _paint(call, text, kb)
    await call.answer()


@router.callback_query(F.data == "adm:stats")
async def admin_stats(call: CallbackQuery) -> None:
    if not await _deny(call):
        return
    text, kb = await _stats_screen(call.bot.db)
    await _paint(call, text, kb)
    await call.answer()


@router.callback_query(F.data == "adm:health")
async def admin_health(call: CallbackQuery) -> None:
    if not await _deny(call):
        return
    text, kb = await _health_screen(call.bot.db)
    await _paint(call, text, kb)
    await call.answer()


@router.callback_query(F.data.startswith("adm:users:"))
async def admin_users(call: CallbackQuery) -> None:
    if not await _deny(call):
        return
    page = max(0, int(call.data.rsplit(":", 1)[1]))
    rows = await call.bot.db.admin_recent_users(PAGE_SIZE, page * PAGE_SIZE)
    text, kb = _users_screen(rows, page)
    await _paint(call, text, kb)
    await call.answer()


@router.callback_query(F.data == "adm:find")
async def admin_find_prompt(call: CallbackQuery, state: FSMContext) -> None:
    if not await _deny(call):
        return
    await state.set_state(AdminLookup.waiting_id)
    await _paint(
        call,
        "🔎 <b>Найти юзера</b>\n\nОтправь его Telegram ID цифрами.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Меню", callback_data="adm:menu")],
        ]),
    )
    await call.answer()


@router.callback_query(F.data.startswith("adm:u:"))
async def admin_user_card(call: CallbackQuery) -> None:
    if not await _deny(call):
        return
    # "adm:u:<page>:<tg_id>" has FOUR parts. Unpacking into three names was a
    # crash on the very first press of the button - and the wiring audit could
    # not see it, because it only checks that a handler exists.
    parts = call.data.split(":")
    page, tg = parts[2], parts[3]
    card = await call.bot.db.admin_user_card(int(tg))
    if not card:
        await call.answer(NOT_FOUND, show_alert=True)
        return
    text, kb = _card_screen(card, int(page))
    await _paint(call, text, kb)
    await call.answer()


@router.callback_query(F.data.startswith(("adm:ban:", "adm:unban:")))
async def admin_ban_toggle(call: CallbackQuery) -> None:
    if not await _deny(call):
        return
    ban = call.data.startswith("adm:ban:")
    tg_id = int(call.data.rsplit(":", 1)[1])

    if ban and tg_id in settings.admin_ids:
        # Locking yourself out needs no cleverness to be irreversible.
        await call.answer("Себя банить нельзя.", show_alert=True)
        return

    state_now = await call.bot.db.set_banned(tg_id, ban)
    if state_now is None:
        await call.answer(NOT_FOUND, show_alert=True)
        return

    logger.info("admin_ban_changed", tg_id=tg_id, banned=state_now, by=call.from_user.id)
    # The ban cache would keep silencing this account for up to its TTL, so the
    # decision has to take effect on the very next message.
    forget_ban(tg_id)
    card = await call.bot.db.admin_user_card(tg_id)
    if card:
        text, kb = _card_screen(card, 0)
        await _paint(call, text, kb)
    await call.answer("Забанен." if state_now else "Разбанен.")


@router.callback_query(F.data == "adm:chaos")
@router.callback_query(F.data.startswith("adm:chaos:"))
async def admin_chaos(call: CallbackQuery) -> None:
    if not await _deny(call):
        return
    from app.services.chaos import (
        disable_chaos, enable_chaos, get_chaos_status,
    )

    action = call.data.rsplit(":", 1)[1] if call.data.count(":") > 1 else ""
    if action == "on":
        enable_chaos()
    elif action == "off":
        disable_chaos()

    status = get_chaos_status()
    text, kb = _chaos_screen(status)
    await _paint(call, text, kb)
    await call.answer(
        "Включено" if status.get("enabled") else "Выключено")


@router.message(StateFilter(AdminLookup.waiting_id), F.text.regexp(r"^\d{5,15}$"))
async def admin_lookup(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await message.answer(ADMIN_ONLY)
        await state.clear()
        return
    await state.clear()
    card = await message.bot.db.admin_user_card(int(message.text))
    if not card:
        await message.answer(NOT_FOUND)
        return
    text, kb = _card_screen(card, 0)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(StateFilter(AdminLookup.waiting_id))
async def admin_lookup_cancelled(message: Message, state: FSMContext) -> None:
    """Any non-numeric message ends lookup mode, and is then handled normally."""
    await state.clear()
    if is_admin(message.from_user.id):
        await message.answer(LOOKUP_CANCELLED)


__all__ = ["router", "admin_keyboard", "is_admin", "ADMIN_BUTTON", "AdminLookup"]