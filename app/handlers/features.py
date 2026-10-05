"""Feature handlers: clan, inline, ml_recommendations. Chaos lives in the admin panel."""

import time
import random
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineQuery, InlineQueryResultArticle, InputTextMessageContent
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from app.config import get_settings
from app.utils.logging import get_logger
from app.utils.formatting import format_kg, escape_html, truncate
from app.utils.validators import validate_username
from app.services import (
    get_clan_service, get_ml_service, get_achievement_service,
    get_daily_bonus_service,
)

router = Router()
logger = get_logger(__name__)
settings = get_settings()


# ===== ML Recommendations handlers =====

@router.message(Command("recommend"))
async def recommend_handler(message: Message):
    """Get personalized recommendations."""
    ml_service = get_ml_service(message.bot.db)
    recommendations = await ml_service.get_cached_recommendations(message.from_user.id)

    if not recommendations:
        await message.answer(
            "🤖 <b>Персональные рекомендации</b>\n\n"
            "Пока не хватает данных для рекомендаций.\n"
            "Начни копать (/dig) и забирай бонусы (/daily)!\n\n"
            "Через несколько дней появится анализ твоего стиля игры.",
            parse_mode="HTML",
        )
        return

    lines = ["🤖 <b>Персональные рекомендации</b>\n"]

    for i, rec in enumerate(recommendations, 1):
        priority_emoji = "🔥" if rec.priority >= 8 else "⭐" if rec.priority >= 5 else "💡"
        lines.append(
            f"{i}. {priority_emoji} <b>{rec.title}</b>\n"
            f"   {escape_html(rec.description)}\n"
        )

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data="recommend_refresh")
    kb.button(text="📊 Профиль", callback_data="recommend_profile")
    kb.adjust(2)

    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(F.data == "recommend_refresh")
async def recommend_refresh_callback(callback: CallbackQuery):
    ml_service = get_ml_service(callback.bot.db)
    await ml_service.invalidate_cache(callback.from_user.id)
    recommendations = await ml_service.generate_recommendations(callback.from_user.id)

    if not recommendations:
        await callback.answer("Недостаточно данных", show_alert=True)
        return

    lines = ["🤖 <b>Персональные рекомендации</b> (обновлено)\n"]
    for i, rec in enumerate(recommendations, 1):
        priority_emoji = "🔥" if rec.priority >= 8 else "⭐" if rec.priority >= 5 else "💡"
        lines.append(
            f"{i}. {priority_emoji} <b>{rec.title}</b>\n"
            f"   {escape_html(rec.description)}\n"
        )

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data="recommend_refresh")
    kb.button(text="📊 Профиль", callback_data="recommend_profile")
    kb.adjust(2)

    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup())
    await callback.answer("Обновлено!")


@router.callback_query(F.data == "recommend_profile")
async def recommend_profile_callback(callback: CallbackQuery):
    ml_service = get_ml_service(callback.bot.db)
    profile = await ml_service.get_user_profile(callback.from_user.id)

    if not profile:
        await callback.answer("Профиль не найден", show_alert=True)
        return

    clan_service = get_clan_service(callback.bot.db)
    clan = await clan_service.get_clan(profile.clan_id) if profile.clan_id else None

    lines = [
        "📊 <b>Твой профиль</b>\n",
        f"🥔 Всего кг: <b>{format_kg(profile.total_kg)}</b>",
        f"⛏ Копок: <b>{profile.digs_count}</b>",
        f"📊 Средний вес: <b>{profile.avg_kg_per_dig:.1f} кг</b>",
        f"🔥 Стрик: <b>{profile.streak} дн.</b>",
        f"🏅 Достижений: <b>{profile.achievements_count}</b>",
    ]

    if profile.favorite_hour is not None:
        lines.append(f"⏰ Любимое время: <b>{profile.favorite_hour}:00</b>")

    if profile.favorite_day is not None:
        days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        lines.append(f"📅 Любимый день: <b>{days[profile.favorite_day]}</b>")

    if clan:
        lines.append(f"🏷 Клан: <b>{escape_html(clan.name)}</b> [{escape_html(clan.tag)}]")

    kb = InlineKeyboardBuilder()
    kb.button(text="🔙 К рекомендациям", callback_data="recommend_refresh")

    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup())
    await callback.answer()


# Smart notification handler (called from other handlers)
async def send_smart_notification(bot, user_id: int):
    """Send a smart notification based on user behavior."""
    ml_service = get_ml_service(bot.db)
    profile = await ml_service.get_user_profile(user_id)

    if not profile:
        return

    # Check if user hasn't played in 24 hours
    import time
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
            pass  # User blocked bot


# Integration with dig handler - send recommendation after dig
async def maybe_send_recommendation(bot, user_id: int):
    """Maybe send a recommendation after dig (10% chance)."""
    if random.random() < 0.1:
        ml_service = get_ml_service(bot.db)
        recommendations = await ml_service.get_cached_recommendations(user_id)

        if recommendations:
            rec = recommendations[0]
            try:
                await bot.send_message(
                    user_id,
                    f"🤖 <b>Умный совет:</b>\n\n"
                    f"{escape_html(rec.title)}\n{escape_html(rec.description)}",
                    parse_mode="HTML",
                )
            except Exception:
                pass


# ===== Clan handlers =====

@router.message(Command("clan"))
async def clan_handler(message: Message):
    clan_service = get_clan_service(message.bot.db)
    clan = await clan_service.get_user_clan(message.from_user.id)

    if not clan:
        kb = InlineKeyboardBuilder()
        kb.button(text="🏷 Создать клан", callback_data="clan_create")
        kb.button(text="📨 Принять приглашение", callback_data="clan_invites")
        await message.answer(
            "🏷 <b>У тебя нет клана</b>\n\n"
            "Создай свой клан или прими приглашение от друзей!",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    members = await clan_service.get_clan_members(clan.clan_id)
    member_lines = []
    for m in members[:10]:
        role_emoji = "👑" if m.role == "leader" else "👮" if m.role == "officer" else "👤"
        member_lines.append(f"  {role_emoji} {m.username} — {format_kg(m.total_kg)} кг")

    lines = [
        f"🏷 <b>{escape_html(clan.name)}</b> [{escape_html(clan.tag)}]\n",
        f"👑 Владелец: <b>{clan.owner_username}</b>",
        f"👥 Участников: <b>{len(members)}/30</b>",
        f"🥔 Общий вес: <b>{format_kg(clan.total_kg)} кг</b>",
        f"📝 Описание: {escape_html(clan.description) or '—'}",
        "\n👥 <b>Участники:</b>",
    ]
    lines.extend(member_lines)

    kb = InlineKeyboardBuilder()
    if clan.owner_id == message.from_user.id:
        kb.button(text="✏️ Редактировать", callback_data="clan_edit")
        kb.button(text="👑 Передать владение", callback_data="clan_transfer")
    elif any(m.tg_id == message.from_user.id and m.role == "officer" for m in members):
        kb.button(text="✏️ Редактировать", callback_data="clan_edit")
    kb.button(text="📨 Пригласить", callback_data="clan_invite")
    kb.button(text="🚪 Покинуть", callback_data="clan_leave")
    kb.button(text="🏆 Топ кланов", callback_data="clan_top")
    kb.adjust(2, 2, 1)

    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup())


@router.message(Command("clan_create"))
async def clan_create_handler(message: Message):
    # Parse: /clan_create Name | Tag | Description
    text = message.text.replace("/clan_create", "").strip()
    if not text or "|" not in text:
        await message.answer(
            "🏷 <b>Создание клана</b>\n\n"
            "Формат: <code>/clan_create Название | ТЕГ | Описание</code>\n\n"
            "ТЕГ — 3-5 символов, только буквы и цифры.\n"
            "Например: <code>/clan_create Картофель | POT | Лучшие копатели</code>",
            parse_mode="HTML",
        )
        return

    parts = [p.strip() for p in text.split("|")]
    name = parts[0]
    tag = parts[1].upper() if len(parts) > 1 else name[:5].upper()
    description = parts[2] if len(parts) > 2 else ""

    if len(tag) < 3 or len(tag) > 5:
        await message.answer("❌ ТЕГ должен быть 3-5 символов")
        return

    if not tag.isalnum():
        await message.answer("❌ ТЕГ должен содержать только буквы и цифры")
        return

    clan_service = get_clan_service(message.bot.db)

    try:
        clan = await clan_service.create_clan(
            owner_id=message.from_user.id,
            name=name,
            tag=tag,
            description=description,
            owner_username=message.from_user.username or message.from_user.first_name,
        )
        await message.answer(
            f"🏷 <b>Клан создан!</b>\n\n"
            f"Название: <b>{escape_html(clan.name)}</b>\n"
            f"Тэг: <b>[{escape_html(clan.tag)}]</b>\n"
            f"Описание: {escape_html(description) or '—'}",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error("clan_create_error", error=str(e))
        await message.answer("❌ Не удалось создать клан. Возможно, такой тэг уже занят.")


@router.message(Command("clan_invite"))
async def clan_invite_handler(message: Message):
    # Parse: /clan_invite @username
    text = message.text.replace("/clan_invite", "").strip()
    if not text:
        await message.answer(
            "📨 <b>Приглашение в клан</b>\n\n"
            "Формат: <code>/clan_invite @username</code>",
            parse_mode="HTML",
        )
        return

    username = validate_username(text)
    if not username:
        await message.answer("❌ Неверный юзернейм")
        return

    clan_service = get_clan_service(message.bot.db)
    clan = await clan_service.get_user_clan(message.from_user.id)

    if not clan:
        await message.answer("❌ У тебя нет клана")
        return

    member = await clan_service.get_member(clan.clan_id, message.from_user.id)
    if not member or member.role not in ("leader", "officer"):
        await message.answer("❌ Только лидер и офицеры могут приглашать")
        return

    target_user = await message.bot.db.get_user_by_tg_id(username)
    if not target_user:
        await message.answer("❌ Пользователь не найден (должен запустить бота)")
        return

    try:
        await clan_service.invite_user(clan.clan_id, target_user.user_id, message.from_user.id)
        await message.answer(f"📨 Приглашение отправлено @{username}")
    except Exception as e:
        logger.error("clan_invite_error", error=str(e))
        await message.answer("❌ Не удалось отправить приглашение")


@router.message(Command("clan_invites"))
async def clan_invites_handler(message: Message):
    clan_service = get_clan_service(message.bot.db)
    invites = await clan_service.get_user_invites(message.from_user.id)

    if not invites:
        await message.answer("📨 Входящих приглашений нет")
        return

    lines = ["📨 <b>Входящие приглашения</b>\n"]
    kb = InlineKeyboardBuilder()

    for inv in invites:
        clan = await clan_service.get_clan(inv.clan_id)
        if clan:
            lines.append(f"🏷 <b>{escape_html(clan.name)}</b> [{escape_html(clan.tag)}] — {inv.inviter_username}")
            kb.button(
                text=f"✅ Принять {escape_html(clan.tag)}",
                callback_data=f"clan_accept_{inv.invite_id}"
            )
            kb.button(
                text=f"❌ Отклонить {escape_html(clan.tag)}",
                callback_data=f"clan_reject_{inv.invite_id}"
            )

    kb.adjust(2)
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("clan_accept_"))
async def clan_accept_callback(callback: CallbackQuery):
    invite_id = int(callback.data.split("_")[2])
    clan_service = get_clan_service(callback.bot.db)
    try:
        clan = await clan_service.accept_invite(invite_id, callback.from_user.id)
        await callback.message.edit_text(
            f"✅ <b>Ты вступил в клан!</b>\n\n"
            f"🏷 <b>{escape_html(clan.name)}</b> [{escape_html(clan.tag)}]",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error("clan_accept_error", error=str(e))
        await callback.answer("❌ Не удалось вступить", show_alert=True)


@router.callback_query(F.data.startswith("clan_reject_"))
async def clan_reject_callback(callback: CallbackQuery):
    invite_id = int(callback.data.split("_")[2])
    clan_service = get_clan_service(callback.bot.db)
    await clan_service.reject_invite(invite_id)
    await callback.message.edit_text("❌ Приглашение отклонено")
    await callback.answer("Отклонено")


@router.callback_query(F.data == "clan_leave")
async def clan_leave_callback(callback: CallbackQuery):
    clan_service = get_clan_service(callback.bot.db)
    clan = await clan_service.get_user_clan(callback.from_user.id)

    if not clan:
        await callback.answer("Ты не в клане", show_alert=True)
        return

    member = await clan_service.get_member(clan.clan_id, callback.from_user.id)
    if member and member.role == "leader":
        await callback.answer("Лидер не может покинуть клан. Передай владение /clan_transfer", show_alert=True)
        return

    await clan_service.leave_clan(clan.clan_id, callback.from_user.id)
    await callback.message.edit_text("🚪 Ты покинул клан")
    await callback.answer("Покинул клан")


@router.message(Command("clan_transfer"))
async def clan_transfer_handler(message: Message):
    text = message.text.replace("/clan_transfer", "").strip()
    if not text:
        await message.answer(
            "👑 <b>Передача владения кланом</b>\n\n"
            "Формат: <code>/clan_transfer @username</code>",
            parse_mode="HTML",
        )
        return

    username = validate_username(text)
    if not username:
        await message.answer("❌ Неверный юзернейм")
        return

    clan_service = get_clan_service(message.bot.db)
    clan = await clan_service.get_user_clan(message.from_user.id)

    if not clan or clan.owner_id != message.from_user.id:
        await message.answer("❌ Только владелец может передать клан")
        return

    target_user = await message.bot.db.get_user_by_tg_id(username)
    if not target_user:
        await message.answer("❌ Пользователь не найден")
        return

    try:
        await clan_service.transfer_ownership(clan.clan_id, target_user.user_id)
        await message.answer(f"👑 Владение кланом передано @{username}")
    except Exception as e:
        logger.error("clan_transfer_error", error=str(e))
        await message.answer("❌ Не удалось передать владение")


@router.message(Command("clan_top"))
async def clan_top_handler(message: Message):
    clan_service = get_clan_service(message.bot.db)
    clans = await clan_service.get_top_clans(10)

    if not clans:
        await message.answer("🏆 Топ кланов пуст")
        return

    lines = ["🏆 <b>Топ кланов</b>\n"]
    for i, clan in enumerate(clans, 1):
        lines.append(f"{i}. 🏷 <b>{escape_html(clan.name)}</b> [{escape_html(clan.tag)}] — {format_kg(clan.total_kg)} кг ({len(await clan_service.get_clan_members(clan.clan_id))} чел.)")

    await message.answer("\n".join(lines), parse_mode="HTML")


# Clan callbacks
@router.callback_query(F.data == "clan_create")
async def clan_create_callback(callback: CallbackQuery):
    await callback.message.answer(
        "🏷 <b>Создание клана</b>\n\n"
        "Формат: <code>/clan_create Название | ТЕГ | Описание</code>\n\n"
        "ТЕГ — 3-5 символов, только буквы и цифры.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "clan_invites")
async def clan_invites_callback(callback: CallbackQuery):
    await clan_invites_handler(callback.message)


@router.callback_query(F.data == "clan_top")
async def clan_top_callback(callback: CallbackQuery):
    await clan_top_handler(callback.message)


@router.callback_query(F.data == "clan_edit")
async def clan_edit_callback(callback: CallbackQuery):
    await callback.answer("Редактирование пока не реализовано", show_alert=True)


@router.callback_query(F.data == "clan_transfer")
async def clan_transfer_callback(callback: CallbackQuery):
    await callback.message.answer(
        "👑 <b>Передача владения кланом</b>\n\n"
        "Формат: <code>/clan_transfer @username</code>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "clan_invite")
async def clan_invite_callback(callback: CallbackQuery):
    await callback.message.answer(
        "📨 <b>Приглашение в клан</b>\n\n"
        "Формат: <code>/clan_invite @username</code>",
        parse_mode="HTML",
    )
    await callback.answer()


# ===== Inline handlers =====

@router.inline_query()
async def inline_query_handler(inline_query: InlineQuery):
    query = inline_query.query.strip().lower()
    user_id = inline_query.from_user.id

    clan_service = get_clan_service(inline_query.bot.db)
    user = await inline_query.bot.db.get_user_by_tg_id(user_id)

    if not user:
        return

    results = []

    # Search clans
    if query:
        clans = await clan_service.search_clans(query, limit=5)
        for clan in clans:
            members = await clan_service.get_clan_members(clan.clan_id)
            results.append(InlineQueryResultArticle(
                id=f"clan_{clan.clan_id}",
                title=f"🏷 {escape_html(clan.name)} [{escape_html(clan.tag)}]",
                description=f"Участников: {len(members)}/30 | Вес: {format_kg(clan.total_kg)} кг",
                input_message_content=InputTextMessageContent(
                    message_text=(
                        f"🏷 <b>{escape_html(clan.name)}</b> [{escape_html(clan.tag)}]\n"
                        f"👥 Участников: {len(members)}/30\n"
                        f"🥔 Вес: {format_kg(clan.total_kg)} кг\n"
                        f"📝 {escape_html(clan.description) or '—'}"
                    ),
                    parse_mode="HTML",
                ),
            ))

    # User stats
    if not query or "стат" in query or "stats" in query:
        results.append(InlineQueryResultArticle(
            id=f"stats_{user_id}",
            title="📊 Моя статистика",
            description=f"Вес: {format_kg(user.total_kg)} кг",
            input_message_content=InputTextMessageContent(
                message_text=(
                    f"📊 <b>Статистика @{user.username or user.first_name}</b>\n"
                    f"🥔 Вес: {format_kg(user.total_kg)} кг"
                ),
                parse_mode="HTML",
            ),
        ))

    # Daily bonus
    if not query or "бонус" in query or "daily" in query:
        daily_bonus_service = get_daily_bonus_service()
        daily_bonus_service.db = inline_query.bot.db
        status = await daily_bonus_service.get_status(user_id)
        results.append(InlineQueryResultArticle(
            id=f"daily_{user_id}",
            title="🎁 Ежедневный бонус",
            description=f"Стрик: {status.get('streak', 0)} дн. | {'Доступен!' if status.get('can_claim') else 'Недоступен'}",
            input_message_content=InputTextMessageContent(
                message_text=(
                    f"🎁 <b>Ежедневный бонус</b>\n"
                    f"🔥 Стрик: {status.get('streak', 0)} дн.\n"
                    f"{'✅ Можно забрать!' if status.get('can_claim') else '⏳ Следующий через: {}ч'.format(status.get('wait_seconds', 0) // 3600)}"
                ),
                parse_mode="HTML",
            ),
        ))

    await inline_query.answer(results, cache_time=1, is_personal=True)
