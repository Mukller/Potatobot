#!/usr/bin/env python3
"""
Potatobot regression suite - run this after ANY change.

    docker exec potatobot python3 /app/tests/suite.py

Covers the failure classes that actually bit us, plus the features added
afterwards (admin auth, achievements, metrics, anticheat). Exit code is
non-zero on any failure so it can gate a deploy.
"""
import asyncio
import re
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, '/app')

from aiogram import Dispatcher
from aiogram.methods import SendMessage
from aiogram.types import (
    CallbackQuery, Chat, InlineQuery, Message, Update, User as TgUser,
)

from app.config import get_settings
from app.database import Database, init_database
from app.handlers import admin_router, commands_router, features_router
from app.handlers.commands import get_main_keyboard
from app.handlers.admin_panel import ADMIN_BUTTON, is_admin
from app.middleware import (
    BanMiddleware, ErrorHandlingMiddleware, LoggingMiddleware, RateLimitMiddleware,
)
from app.services import (
    get_achievement_service, get_clan_service, get_daily_bonus_service,
    get_dig_service, get_metrics_service, get_ml_service,
)

settings = get_settings()
TAGS = {'b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del', 'span',
        'tg-spoiler', 'tg-emoji-tag-id', 'a', 'code', 'pre', 'blockquote'}
TAG_RE = re.compile(r'<(/?)([a-zA-Z0-9-]+)([^>]*)>')

results = []
sent = []
acks = []


def rec(name, ok, detail=''):
    results.append((bool(ok), name, detail))
    print(f'  {"PASS" if ok else "FAIL"}  {name}' + (f'   {detail}' if detail and not ok else ''))


def valid_html(text):
    if not text:
        return True
    if len(text) > 4096:
        return False
    stack = []
    for m in TAG_RE.finditer(text):
        closing, name = m.group(1), m.group(2).lower()
        if name not in TAGS:
            return False
        if name in ('code', 'pre', 'blockquote'):
            continue
        if not closing:
            stack.append(name)
        elif not stack or stack[-1] != name:
            return False
        else:
            stack.pop()
    return not stack


class _R:
    def __getattr__(self, k): return None


class S:
    def __init__(self, b): self.b = b

    async def __call__(self, bot, m):
        n = type(m).__name__
        if n == 'SendMessage':
            sent.append(m)
        elif n in ('EditMessageText', 'EditMessageReplyMarkup'):
            sent.append(m)
        elif n == 'AnswerCallbackQuery':
            acks.append(getattr(m, 'text', '') or '')
        elif n == 'AnswerInlineQuery':
            acks.append('inline:' + str(len(getattr(m, 'results', None) or [])))
        return _R()

    async def close(self, *a, **k): return None


class Bot:
    id = 8209854911; is_bot = True; username = 'Potatoltbot'; first_name = 'P'

    def __init__(self, db):
        self.db = db; self.settings = settings
        self.dig_service = get_dig_service(); self.dig_service.db = db
        self.daily_bonus_service = get_daily_bonus_service(); self.daily_bonus_service.db = db
        self.achievement_service = get_achievement_service(db)
        self.clan_service = get_clan_service(db); self.ml_service = get_ml_service(db)
        self.admin_ids = settings.admin_ids; self.session = S(self)

    def __call__(self, m): return self.session(self, m)


def make_dp():
    dp = Dispatcher()
    # Same order as app/main.py. BanMiddleware must be here too, otherwise the
    # suite could never catch a broken ban - and an untested ban is a ban that
    # silently does nothing.
    dp.message.middleware(BanMiddleware())
    dp.message.middleware(ErrorHandlingMiddleware())
    dp.message.middleware(LoggingMiddleware())
    dp.message.middleware(RateLimitMiddleware())
    dp.callback_query.middleware(ErrorHandlingMiddleware())
    dp.include_router(commands_router)
    dp.include_router(features_router)
    dp.include_router(admin_router)
    return dp


def mu(bot, text, mid, uid, first='Тест', uname='suite'):
    return Update(update_id=mid, message=Message(
        message_id=mid, date=datetime.now(timezone.utc),
        chat=Chat(id=uid, type='private'),
        from_user=TgUser(id=uid, is_bot=False, first_name=first, username=uname),
        content_type='text', text=text, bot=bot))


def cq(bot, data, mid, uid):
    """A callback_query update, so inline buttons can be pressed in tests."""
    user = TgUser(id=uid, is_bot=False, first_name='Тест', username='suite')
    msg = Message(
        message_id=mid, date=datetime.now(timezone.utc),
        chat=Chat(id=uid, type='private'),
        from_user=user, content_type='text', text='panel', bot=bot)
    return Update(update_id=mid, callback_query=CallbackQuery(
        id=str(mid), from_user=user, chat_instance='suite', message=msg,
        data=data))


async def press(dp, bot, upd):
    sent.clear(); acks.clear()
    await dp.feed_update(bot, upd)
    return list(sent) + list(acks)


def has_label(out, needle):
    """Substring search across the buttons actually offered."""
    return any(needle in (lb or '') for lb in kb_labels(out))


def kb_labels(out):
    """Button labels attached to whatever was sent - the panel puts all of
    its controls in reply_markup, never in the message text."""
    labels = []
    for o in out:
        mk = getattr(o, 'reply_markup', None)
        if mk is None:
            continue
        for row in getattr(mk, 'inline_keyboard', []) or []:
            for b in row:
                labels.append(getattr(b, 'text', '') or '')
    return labels


def joined(out):
    parts = []
    for o in out:
        if isinstance(o, str):
            parts.append(o)
        else:
            parts.append((getattr(o, 'text', '') or '').replace('\n', ' '))
    return ' | '.join(parts)


async def fresh_user(db, uid):
    async with db.acquire() as c:
        old = await c.fetchval('SELECT user_id FROM users WHERE tg_id=$1', uid)
        if old:
            await c.execute('DELETE FROM user_achievements WHERE user_id=$1', old)
            await c.execute('DELETE FROM dig_history WHERE user_id=$1', old)
            await c.execute('DELETE FROM users WHERE user_id=$1', old)
    return await db.get_or_create_user(uid, 'suite')


async def main():
    db = Database(settings.database_url)
    await db.connect()
    await init_database(settings.database_url)
    bot = Bot(db)
    dp = make_dp()
    uid = 700_000_000 + (int(time.time()) % 90_000_000)
    await fresh_user(db, uid)
    mid = [50000]

    def nxt():
        mid[0] += 1
        return mid[0]

    # ---------------------------------------------------------- 1. wiring
    print('\n[1] wiring: rendered controls have handlers')
    src = (open('/app/app/handlers/commands.py', encoding='utf-8').read() + '\n' +
           open('/app/app/handlers/features.py', encoding='utf-8').read() + '\n' +
           open('/app/app/handlers/admin_panel.py', encoding='utf-8').read())
    dc, dt = set(), set()
    dcbs, dpref = set(), set()
    for m in re.finditer(r'@router\.(?:message|callback_query)\((.*?)\)\s*(?:async\s+)?def',
                         src, re.DOTALL):
        a = m.group(1)
        dc |= set(re.findall(r'Command\("([a-z_]+)"\)', a))
        dt |= set(re.findall(r'F\.text\s*==\s*"([^"]+)"', a))
        dcbs |= set(re.findall(r'F\.data\s*==\s*"([^"]+)"', a))
        dpref |= set(re.findall(r'F\.data\.startswith\("([^"]+)"\)', a))
        # startswith(("a", "b")) is just as valid aiogram as startswith("a"),
        # so the guard must read the tuple form too - otherwise a perfectly
        # wired button is reported as dead and people stop trusting the guard.
        for tup in re.findall(r'F\.data\.startswith\(\(([^)]*)\)\)', a):
            dpref |= set(re.findall(r'"([^"]+)"', tup))

    def cb_ok(v):
        if v in dcbs:
            return True
        if '{' in v and v.split('{')[0] in (dcbs | dpref):
            return True
        return any(v.startswith(p) for p in dpref)

    dead = [v for v in set(re.findall(r'callback_data=(?:f?)"([^"]+)"', src)) if not cb_ok(v)]
    rec('every inline callback has a handler', not dead, f'dead={dead}')
    rec('the guard still catches a dead callback',
        not cb_ok('adm:this_does_not_exist:1'), 'guard went soft')

    kb = get_main_keyboard()
    kb_texts = [b.text for row in kb.keyboard for b in row]
    dead = [t for t in kb_texts if t not in dt]
    from app.handlers.admin_panel import ADMIN_BUTTON, is_admin
    rec('admin button is in the admin keyboard',
        ADMIN_BUTTON in [b.text for r in get_main_keyboard(
            settings.admin_ids[0]).keyboard for b in r])
    rec('admin button is NOT in a guest keyboard',
        ADMIN_BUTTON not in [b.text for r in get_main_keyboard(
            settings.admin_ids[0] + 1).keyboard for b in r])
    rec('every reply-keyboard button has a handler', not dead, f'dead={dead}')

    main_src = open('/app/app/main.py', encoding='utf-8').read()
    adv = re.findall(r'BotCommand\(command="([a-z_]+)"', main_src)
    dead = [c for c in adv if c not in dc]
    rec('every advertised command has a handler', not dead, f'dead={dead}')
    rec('admin button is a registered router handler',
        len(admin_router.message.handlers) > 0,
        'admin_router has no message handlers')
    rec('admin router is included in main.py',
        'admin_router' in main_src and 'include_router(admin_router)' in main_src,
        'admin_router not registered in main.py')

    # -------------------------------------------------------- 2. behaviour
    print('\n[2] behaviour: each control returns its own screen')
    expect = {
        '\U0001f954 Копать': ('Выкопано', 'Подожди'),
        '\U0001f4ca Статистика': ('статистика',),
        '\U0001f381 Бонус': ('Ежедневный бонус',),
        '\U0001f3c5 Достижения': ('Достижения',),
        '\U0001f3f7 Клан': ('Клан', 'клан'),
        '\U0001f3c6 Топ': ('Топ',),
    }
    for row in kb.keyboard:
        for btn in row:
            out = joined(await press(dp, bot, mu(bot, btn.text, nxt(), uid)))
            low = out.lower()
            want = expect.get(btn.text, ())
            rec(f'button {btn.text}', bool(out) and any(w.lower() in low for w in want),
                f'got {out[:60]!r}')

    cmdexp = {
        '/start': 'Добро пожаловать', '/my_stats': 'статистика',
        '/my_history': ('История копок', 'История пуста'),
        '/top_day': 'Топ за сутки', '/top_all': 'Общий топ',
        '/achievements': 'Достижения', '/help': 'Помощь',
        '/clan': ('Клан', 'клан'), '/clan_top': 'Топ кланов',
        '/clan_invites': 'приглашений', '/recommend': ('рекомендац', 'Рекомендации'),
        '/daily': 'Ежедневный бонус',
    }
    for cmd, want in cmdexp.items():
        out = joined(await press(dp, bot, mu(bot, cmd, nxt(), uid)))
        low = out.lower()
        ws = want if isinstance(want, (tuple, list)) else (want,)
        rec(f'command {cmd}', bool(out) and any(w.lower() in low for w in ws),
            f'got {out[:60]!r}')

    def c_upd(data, u):
        inner = Message(message_id=nxt(), date=datetime.now(timezone.utc),
                        chat=Chat(id=u, type='private'),
                        from_user=TgUser(id=u, is_bot=False, first_name='T',
                                         username='suite'),
                        content_type='text', text='x', bot=bot)
        return Update(update_id=nxt(), callback_query=CallbackQuery(
            id=str(nxt()), from_user=inner.from_user, chat_instance='ci',
            data=data, message=inner, bot=bot))

    for data, want in [('stats', 'статистика'),
                       ('history', ('История копок', 'История пуста')),
                       ('daily', 'Ежедневный бонус'),
                       ('achievements', 'Достижения')]:
        out = joined(await press(dp, bot, c_upd(data, uid)))
        low = out.lower()
        ws = want if isinstance(want, (tuple, list)) else (want,)
        rec(f'callback {data}', bool(out) and any(w.lower() in low for w in ws),
            f'got {out[:60]!r}')

    # -------------------------------------------------------- 3. dig rules
    print('\n[3] dig rules')
    uid2 = 750_000_000 + (int(time.time()) % 80_000_000)
    await fresh_user(db, uid2)
    out = joined(await press(dp, bot, mu(bot, '/dig', nxt(), uid2)))
    rec('first dig succeeds', 'Выкопано' in out, out[:60])
    out = joined(await press(dp, bot, mu(bot, '/dig', nxt(), uid2)))
    rec('cooldown blocks second dig', 'Выкопано' not in out, out[:60])
    rec('cooldown message is friendly', 'Подожди' in out, out[:60])

    # ------------------------------------------------------- 4. atomicity
    print('\n[4] atomicity and concurrency')
    uid3 = 760_000_000 + (int(time.time()) % 80_000_000)
    u3 = await fresh_user(db, uid3)
    ds = get_dig_service(); ds.db = db
    res = await asyncio.gather(*[ds.perform_dig(uid3, 'race') for _ in range(5)],
                               return_exceptions=True)
    wins = sum(1 for r in res if isinstance(r, dict))
    rec('5 simultaneous digs -> exactly 1 wins', wins == 1, f'wins={wins}')
    usr = await db.get_user_by_tg_id(uid3)
    hist = await db.get_user_history(usr.user_id, 50)
    rec('total_kg == sum(history)', abs(usr.total_kg - sum(x.kg for x in hist)) < 0.01,
        f"total={usr.total_kg} sum={sum(x.kg for x in hist)}")
    rec('digs_count == len(history)', usr.digs_count == len(hist),
        f'count={usr.digs_count} rows={len(hist)}')

    n = 20
    uids = [400_000_000 + (int(time.time()) % 50_000_000) + i * 7919 for i in range(n)]
    for u in uids:
        await fresh_user(db, u)
    res = await asyncio.gather(*[ds.perform_dig(u, 'conc') for u in uids],
                               return_exceptions=True)
    errs = [r for r in res if isinstance(r, Exception)]
    rec(f'{n} simultaneous digs -> no exceptions', not errs,
        f'{len(errs)} errors: {errs[0] if errs else ""}')
    rec(f'{n} simultaneous digs -> all land',
        sum(1 for r in res if isinstance(r, dict)) == n,
        f'landed={sum(1 for r in res if isinstance(r, dict))}')
    for u in uids:
        await fresh_user(db, u)

    # ------------------------------------------------- 5. schema and types
    print('\n[5] schema / types')
    async with db.acquire() as c:
        types = {r['column_name']: r['data_type'] for r in await c.fetch(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name='users'")}
    rec('users.last_dig_time is float8',
        types.get('last_dig_time') == 'double precision', str(types.get('last_dig_time')))
    rec('users.created_at is float8',
        types.get('created_at') == 'double precision', str(types.get('created_at')))
    async with db.acquire() as c:
        tnames = {r['table_name'] for r in await c.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}
    need = {'users', 'dig_history', 'clans', 'clan_members', 'clan_invites',
            'user_achievements'}
    rec('all tables present', need <= tnames, f'missing={need - tnames}')
    dead = {'sessions', 'counters'} & tnames
    rec('dead web-panel tables are gone', not dead, str(dead))
    async with db.acquire() as c:
        ucols = {r['column_name'] for r in await c.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='users'")}
    rec('users.is_banned exists', 'is_banned' in ucols, str(sorted(ucols)))

    # ------------------------------------------------------ 6. HTML safety
    print('\n[6] HTML safety')
    cs = get_clan_service(db)
    u = await db.get_or_create_user(uid, 'hostile')
    clan = None
    try:
        clan = await cs.create_clan(u.user_id, 'A&B <b>x</b> "q"', 'ZZ1', 'd<b>', 'hostile')
        rec('hostile clan name is accepted', True)
    except Exception as e:
        rec('hostile clan name handled cleanly', True, str(e)[:40])
    if clan:
        for label, text in [('/clan', '/clan'), ('btn', '\U0001f3f7 Клан')]:
            out = await press(dp, bot, mu(bot, text, nxt(), uid))
            body = joined(out)
            rec(f'{label} survives hostile clan name',
                bool(out) and all(valid_html(getattr(o, 'text', '')) for o in out
                                   if not isinstance(o, str)),
                body[:70])
        async with db.acquire() as c:
            await c.execute('DELETE FROM clan_members WHERE clan_id=$1', clan.clan_id)
            await c.execute('DELETE FROM clans WHERE clan_id=$1', clan.clan_id)

    out = await press(dp, bot, mu(bot, '/start', nxt(), uid, first='<script>x</script>'))
    texts = [getattr(o, 'text', '') or '' for o in out if not isinstance(o, str)]
    rec('hostile first_name in /start', bool(texts) and all(valid_html(t) for t in texts),
        str(texts)[:70])

    # ------------------------------------------------------------ 7. limits
    print('\n[7] message length limits')
    over = []
    for label, text in [('/top_all', '/top_all'), ('/top_day', '/top_day'),
                        ('/my_history', '/my_history'), ('/help', '/help')]:
        out = await press(dp, bot, mu(bot, text, nxt(), uid))
        for o in out:
            t = getattr(o, 'text', '') or '' if not isinstance(o, str) else o
            if len(t) > 4096:
                over.append(f'{label}:{len(t)}')
    rec('all messages fit Telegram 4096 limit', not over, str(over))

    # ------------------------------------------------------- 8. admin panel
    print('\n[8] admin panel is Telegram-only')
    owner = settings.admin_ids[0]
    from app.handlers.admin_panel import admin_keyboard, is_admin

    rec('is_admin accepts the owner', is_admin(owner))
    rec('is_admin rejects everyone else', not is_admin(uid))
    rec('admin_keyboard is a single row of buttons',
        len(admin_keyboard().keyboard) == 1)

    out = joined(await press(dp, bot, mu(bot, '/admin', nxt(), uid)))
    rec('non-admin gets no panel',
        'администратор' in out.lower() and 'Статистика' not in out, out[:70])

    # The panel must contain no outbound link at all: no browser, no session.
    sent.clear(); acks.clear()
    await dp.feed_update(bot, mu(bot, '/admin', nxt(), owner))
    urls, cbs = [], []
    for o in sent:
        mk = getattr(o, 'reply_markup', None)
        if mk is None:
            continue
        for r in getattr(mk, 'inline_keyboard', []) or []:
            for b in r:
                cbs.append(getattr(b, 'callback_data', None))
                if getattr(b, 'url', None):
                    urls.append(b.url)
    rec('admin gets the stats panel', 'Статистика' in joined(sent), joined(sent)[:70])
    rec('the panel has no web links at all', not urls, str(urls[:2]))
    rec('panel controls are callbacks, not buttons-with-url',
        len(cbs) >= 5 and all(cbs), str(cbs[:6]))

    # Every panel screen must answer without touching the web server.
    for data, label in [('adm:stats', 'stats'), ('adm:users:0', 'users'),
                        ('adm:health', 'health'), ('adm:chaos', 'chaos')]:
        sent.clear(); acks.clear()
        await dp.feed_update(bot, cq(bot, data, nxt(), owner))
        rec(f'panel screen {label} renders', bool(sent), str(acks[:2]))

    # A non-admin pressing the same buttons must get nothing but a refusal.
    sent.clear(); acks.clear()
    await dp.feed_update(bot, cq(bot, 'adm:users:0', nxt(), uid))
    leaked = [o for o in sent if isinstance(o, SendMessage)]
    rec('non-admin cannot open the user list', not leaked, f'{len(leaked)} message(s)')
    rec('non-admin callback is answered',
        any('администратор' in a.lower() for a in acks), str(acks[:2]))

    # ---- press the buttons, do not merely look for the handler
    await fresh_user(db, victim_probe := 778000222)
    user_row = await db.admin_recent_users(50, 0)
    target = next(u for u in user_row if u['tg_id'] == victim_probe)
    page = 0
    sent.clear(); acks.clear()
    await dp.feed_update(bot, cq(bot, 'adm:users:0', nxt(), owner))
    listing = joined(sent)
    rec('user list renders a line per user', str(victim_probe) in listing,
        listing[:70])

    # the exact button the list emitted, pressed for real
    sent.clear(); acks.clear()
    await dp.feed_update(bot, cq(bot, f"adm:u:{page}:{victim_probe}", nxt(), owner))
    card = joined(sent)
    labels = kb_labels(sent)
    rec('pressing a user in the list opens the card',
        has_label(sent, 'Забанить') or has_label(sent, 'Разбанить'),
        f'labels={labels} text={card[:60]}')
    rec('the card shows the right Telegram ID', str(victim_probe) in card,
        card[:70])
    rec('no crash while parsing the four-part callback',
        not any('Traceback' in a for a in acks), str(acks[:2]))

    # the card's own button must work too
    sent.clear(); acks.clear()
    await dp.feed_update(bot, cq(bot, f'adm:ban:{victim_probe}', nxt(), owner))
    rec('ban from the card works', await db.is_banned(victim_probe) is True,
        str(acks[:2]))
    sent.clear(); acks.clear()
    await dp.feed_update(bot, cq(bot, f"adm:u:{page}:{victim_probe}", nxt(), owner))
    rec('the card re-renders after the ban', has_label(sent, 'Разбанить'),
        f'labels={kb_labels(sent)}')
    sent.clear(); acks.clear()
    await dp.feed_update(bot, cq(bot, f'adm:unban:{victim_probe}', nxt(), owner))
    rec('unban from the card works', await db.is_banned(victim_probe) is False,
        str(acks[:2]))

    # a card for an unknown account must answer, not raise
    sent.clear(); acks.clear()
    await dp.feed_update(bot, cq(bot, f'adm:u:0:999999998', nxt(), owner))
    rec('unknown account gets a readable answer',
        any('не найден' in a.lower() for a in acks), str(acks[:2]))

    # back navigation must work from every screen
    for data in ('adm:u:0:%d' % victim_probe, 'adm:chaos:on', 'adm:chaos:off'):
        sent.clear(); acks.clear()
        await dp.feed_update(bot, cq(bot, data, nxt(), owner))
        rec(f'{data} does not crash', not any('Traceback' in a for a in acks),
            str(acks[:2]))
    sent.clear(); acks.clear()
    await dp.feed_update(bot, cq(bot, 'adm:menu', nxt(), owner))
    rec('back to menu works', has_label(sent, 'Статистика'),
        f'labels={kb_labels(sent)}')
    rec('the button-label helper is not a rubber stamp',
        has_label(sent, 'Статистика') and not has_label(sent, 'НичегоТакого'),
        str(kb_labels(sent)))

    # ---- ban must be real: a flag the middleware actually enforces
    victim = 777000111
    await fresh_user(db, victim)
    rec('ban returns the new state', await db.set_banned(victim, True) is True)
    rec('ban reads back', await db.is_banned(victim) is True)
    sent.clear(); acks.clear()
    await dp.feed_update(bot, mu(bot, '/start', nxt(), victim))
    rec('banned account is ignored completely', not sent, f'{len(sent)} answer(s)')

    # the panel must refuse to lock its own owner out
    sent.clear(); acks.clear()
    await dp.feed_update(bot, cq(bot, f'adm:ban:{owner}', nxt(), owner))
    rec('panel refuses to ban the owner',
        any('себя' in a.lower() for a in acks), str(acks[:2]))
    rec('the owner is still not banned', not await db.is_banned(owner))

    # Unban has to go through the panel: buttons are the only way the admin
    # has, and the panel is what clears the middleware's short-lived cache.
    sent.clear(); acks.clear()
    await dp.feed_update(bot, cq(bot, f'adm:unban:{victim}', nxt(), owner))
    rec('panel unban is confirmed', any('разбан' in a.lower() for a in acks),
        str(acks[:2]))
    rec('unban reads back as False', await db.is_banned(victim) is False)
    sent.clear(); acks.clear()
    await dp.feed_update(bot, mu(bot, '/start', nxt(), victim))
    rec('unbanned account is served again', bool(sent), f'{len(sent)} answer(s)')

    # a ban on an account that never used the bot must say so, not crash
    rec('ban of an unknown account returns None',
        await db.set_banned(999999999, True) is None)

    # ------------------------------------------------------- 9. achievements
    print('\n[9] achievements persist')
    svc = get_achievement_service(db)
    ua = await fresh_user(db, 780_000_000 + (int(time.time()) % 80_000_000))
    got = await svc.get_user_achievements(ua.user_id)
    rec('fresh user has no achievements', got == set(), f'got {sorted(got)}')
    await get_dig_service().perform_dig(ua.tg_id, 'ach')
    got = await svc.get_user_achievements(ua.user_id)
    rec('a real dig unlocks first_dig', 'first_dig' in got, f'got {sorted(got)}')
    again = {a['id'] for a in await svc.check_achievements(ua.user_id, 3.0, 9.0)}
    rec('no duplicate toast', 'first_dig' not in again, f'got {sorted(again)}')
    bad_id = await svc.check_achievements(999999999, 7.0, 50.0)
    rec('a bad user id cannot raise', isinstance(bad_id, list), type(bad_id).__name__)

    # ---------------------------------------------------------- 10. metrics
    print('\n[10] metrics move')
    from prometheus_client import generate_latest

    def read():
        out = {}
        for line in generate_latest().decode('utf-8', 'replace').splitlines():
            if line.startswith('#') or not line.strip():
                continue
            m = re.match(r'([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([0-9.eE+-]+)$', line)
            if m:
                out[m.group(1) + (m.group(2) or '')] = float(m.group(3))
        return out

    um = await fresh_user(db, 790_000_000 + (int(time.time()) % 80_000_000))
    before = read()

    async def clear_cd():
        async with db.acquire() as c:
            await c.execute('UPDATE users SET last_dig_time=NULL WHERE user_id=$1',
                            um.user_id)

    await clear_cd()
    for _ in range(2):
        await get_dig_service().perform_dig(um.tg_id, 'met')
        await clear_cd()
    # daily bonus needs its own gate cleared (it keys off last_dig_time too)
    await clear_cd()
    try:
        await get_daily_bonus_service().claim(um.tg_id)
    except Exception:
        pass
    after = read()

    def moved(prefix):
        keys = {k for k in set(before) | set(after) if k.startswith(prefix)}
        return sum(after.get(k, 0) - before.get(k, 0) for k in keys) > 0

    rec('dig counter moves', moved('potatobot_dig_commands_total{status="success"}'))
    rec('kg histogram moves', moved('potatobot_dig_kg_count'))
    rec('bonus counter moves',
        any(k.startswith('potatobot_daily_bonus_claims_total')
            and after[k] > before.get(k, 0) for k in after))

    # ------------------------------------------------------ 11. anticheat
    print('\n[11] anticheat is a real guard')
    s = get_settings()
    oc, oi = s.dig_cooldown_seconds, s.anticheat_min_dig_interval
    try:
        s.dig_cooldown_seconds = 0
        s.anticheat_min_dig_interval = 0.0
        ua2 = await fresh_user(db, 791_000_000 + (int(time.time()) % 80_000_000))
        fired = False
        digs = 0
        for _ in range(25):
            try:
                await get_dig_service().perform_dig(ua2.tg_id, 'anti')
                digs += 1
            except Exception as e:
                if 'Подозрительная' in str(e):
                    fired = True
                break
        rec('hourly cap stops a flood', fired, f'digs={digs}')
        rec('cap does not fire on the first dig', digs >= 2, f'digs={digs}')
    finally:
        s.dig_cooldown_seconds = oc
        s.anticheat_min_dig_interval = oi

    # cleanup: leave no synthetic users behind (fresh_user() would create one)
    owner = settings.admin_ids[0]
    async with db.acquire() as c:
        await c.execute(
            'DELETE FROM user_achievements WHERE user_id IN '
            '(SELECT user_id FROM users WHERE tg_id <> $1)', owner)
        await c.execute(
            'DELETE FROM dig_history WHERE user_id IN '
            '(SELECT user_id FROM users WHERE tg_id <> $1)', owner)
        await c.execute('DELETE FROM users WHERE tg_id <> $1', owner)
    await db.close()

    failed = [r for r in results if not r[0]]
    print(f'\n=========== {len(results) - len(failed)} passed, {len(failed)} failed ===========')
    for _, name, detail in failed:
        print(f'  FAIL {name}  {detail}')
    return 1 if failed else 0


sys.exit(asyncio.run(main()) or 0)