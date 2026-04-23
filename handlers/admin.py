"""
handlers/admin.py
=================
Административные команды управления рассылкой:

* /setdelay <секунды>             — задержка между группами.
* /setinterval <Nh|Nm|N>          — интервал между циклами (часы/минуты).
* /setrotation <round|random|single> [post_id]
                                   — режим ротации; при 'single' нужен id.
* /sendnow [post_id]              — запустить цикл немедленно.
* /status                         — состояние бота.
"""

from __future__ import annotations

import logging
from html import escape as html_escape
from typing import Optional, Tuple

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from broadcast import BroadcastService
from database import (
    Database,
    KEY_DELAY,
    KEY_LAST_BROADCAST,
    KEY_ROTATION,
    KEY_SINGLE_POST,
)
from scheduler import BroadcastScheduler
from utils import AdminFilter, format_eta, parse_iso_utc


logger = logging.getLogger(__name__)

router = Router(name="admin")
router.message.filter(AdminFilter())


# =============================================================================
# /setdelay
# =============================================================================


@router.message(Command("setdelay"))
async def cmd_setdelay(
    message: Message, command: CommandObject, db: Database
) -> None:
    seconds = _parse_int(command.args)
    if seconds is None or seconds < 1 or seconds > 3600:
        await message.answer(
            "Формат: <code>/setdelay N</code>, где N — от 1 до 3600 секунд.\n\n"
            "Рекомендуемые значения: <b>15–30 сек</b>. Агрессивные 1–5 сек "
            "быстро приводят к FloodWait от Telegram."
        )
        return
    await db.set_setting(KEY_DELAY, str(seconds))
    await message.answer(
        f"✅ Задержка между группами: <b>{seconds} сек</b>."
    )
    logger.info("Задержка между группами изменена на %d сек.", seconds)


# =============================================================================
# /setinterval
# =============================================================================


@router.message(Command("setinterval"))
async def cmd_setinterval(
    message: Message,
    command: CommandObject,
    db: Database,
    broadcast_scheduler: BroadcastScheduler,
) -> None:
    """Принимает:
        /setinterval 240       → 240 минут
        /setinterval 240m      → 240 минут
        /setinterval 6h        → 6 часов = 360 минут
        /setinterval 1d        → 1 день = 1440 минут
    """
    if not command.args:
        await message.answer(
            "Формат: <code>/setinterval 4h</code>, "
            "<code>/setinterval 30m</code> или <code>/setinterval 1d</code>."
        )
        return

    minutes = _parse_interval(command.args)
    if minutes is None or minutes < 1 or minutes > 7 * 24 * 60:
        await message.answer(
            "Не удалось разобрать интервал. Допустимы: N, Nm, Nh, Nd; "
            "от 1 минуты до 7 дней."
        )
        return

    await broadcast_scheduler.reschedule(minutes)
    await message.answer(
        f"✅ Интервал между циклами: <b>{minutes} мин</b> "
        f"(~ {minutes / 60:.1f} ч)."
    )


# =============================================================================
# /setrotation
# =============================================================================


@router.message(Command("setrotation"))
async def cmd_setrotation(
    message: Message, command: CommandObject, db: Database
) -> None:
    if not command.args:
        await message.answer(
            "Формат:\n"
            "• <code>/setrotation round</code> — по кругу\n"
            "• <code>/setrotation random</code> — случайный\n"
            "• <code>/setrotation single ID</code> — один выбранный пост"
        )
        return

    parts = command.args.split(maxsplit=1)
    mode = parts[0].lower()
    if mode not in ("round", "random", "single"):
        await message.answer(
            "Допустимо: <code>round</code>, <code>random</code>, <code>single</code>."
        )
        return

    await db.set_setting(KEY_ROTATION, mode)
    if mode == "single":
        post_id = _parse_int(parts[1] if len(parts) > 1 else None)
        if post_id is None:
            await message.answer(
                "Для режима <b>single</b> укажите ID поста: "
                "<code>/setrotation single 5</code>."
            )
            return
        if await db.get_post(post_id) is None:
            await message.answer(f"Пост #{post_id} не найден.")
            return
        await db.set_setting(KEY_SINGLE_POST, str(post_id))
        await message.answer(
            f"✅ Режим <b>single</b>: всегда рассылается пост #{post_id}."
        )
    else:
        # Чистим привязку к single-post, если была.
        await db.set_setting(KEY_SINGLE_POST, None)
        await message.answer(f"✅ Режим ротации: <b>{mode}</b>.")

    logger.info("Режим ротации изменён на %s.", mode)


# =============================================================================
# /sendnow
# =============================================================================


@router.message(Command("sendnow"))
async def cmd_sendnow(
    message: Message,
    command: CommandObject,
    broadcast: BroadcastService,
) -> None:
    post_id = _parse_int(command.args) if command.args else None
    await message.answer(
        "🚀 Запускаю цикл рассылки… Результат придёт следующим сообщением."
    )
    stats = await broadcast.run_cycle(post_id=post_id, force=True)
    await message.answer(stats.as_text())


# =============================================================================
# /status
# =============================================================================


@router.message(Command("status"))
async def cmd_status(
    message: Message,
    db: Database,
    broadcast_scheduler: BroadcastScheduler,
) -> None:
    delay, interval, rotation = await _fetch_status_numbers(db)
    groups = await db.list_groups(only_active=True)
    posts = await db.list_posts()
    next_run = broadcast_scheduler.next_run_time()
    last_raw = await db.get_setting(KEY_LAST_BROADCAST)
    last_dt = parse_iso_utc(last_raw)
    single_post = await db.get_setting(KEY_SINGLE_POST)

    rotation_detail = rotation
    if rotation == "single" and single_post:
        rotation_detail = f"single (пост #{html_escape(single_post)})"

    last_stamp = (
        last_dt.strftime("%Y-%m-%d %H:%M UTC") if last_dt else "никогда"
    )

    text = (
        "<b>📊 Статус бота</b>\n"
        f"• Активных групп: <b>{len(groups)}</b>\n"
        f"• Сохранённых постов: <b>{len(posts)}</b>\n"
        f"• Задержка между группами: <b>{delay} сек</b>\n"
        f"• Интервал между циклами: <b>{interval} мин</b> (~{interval/60:.1f} ч)\n"
        f"• Режим ротации: <b>{html_escape(rotation_detail)}</b>\n"
        f"• Последний цикл: <b>{last_stamp}</b>\n"
        f"• Следующий цикл: <b>{format_eta(next_run)}</b>"
    )
    await message.answer(text)


async def _fetch_status_numbers(db: Database) -> Tuple[int, int, str]:
    delay = await db.get_delay_seconds()
    interval = await db.get_interval_minutes()
    rotation = await db.get_rotation_mode()
    return delay, interval, rotation


# =============================================================================
# helpers
# =============================================================================


def _parse_int(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _parse_interval(raw: str) -> Optional[int]:
    """Разбирает строки вида '5', '30m', '6h', '1d' в минуты."""
    raw = raw.strip().lower()
    if not raw:
        return None
    multiplier = 1
    if raw.endswith("m"):
        raw = raw[:-1]
    elif raw.endswith("h"):
        multiplier = 60
        raw = raw[:-1]
    elif raw.endswith("d"):
        multiplier = 60 * 24
        raw = raw[:-1]
    try:
        value = int(raw)
    except ValueError:
        return None
    if value <= 0:
        return None
    return value * multiplier
