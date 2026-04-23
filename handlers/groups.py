"""
handlers/groups.py
==================
Команды управления списком групп-получателей.

* /addgroup    — выполняется ВНУТРИ группы. Требуется, чтобы бот был
                 участником и имел право отправлять сообщения.
* /removegroup — выполняется внутри группы (убрать её из рассылки) или
                 в приватном чате администратора в виде
                 `/removegroup <chat_id>`.
* /listgroups  — список активных групп (в приватном чате).
"""

from __future__ import annotations

import logging
from html import escape as html_escape
from typing import Optional

from aiogram import Bot, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from config import settings
from database import Database


logger = logging.getLogger(__name__)

router = Router(name="groups")


# =============================================================================
# /addgroup  — только в группе, только для админа из белого списка
# =============================================================================


@router.message(Command("addgroup"))
async def cmd_addgroup(message: Message, db: Database, bot: Bot) -> None:
    # Команда должна вызываться ВНУТРИ группы/супергруппы.
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.answer(
            "Эту команду надо выполнять внутри группы, куда добавлен бот."
        )
        return

    # Отправитель команды должен быть администратором бота.
    if message.from_user is None or not settings.is_admin(message.from_user.id):
        # Молча игнорируем, чтобы не светить факт существования бота
        # случайным участникам группы.
        return

    # Проверяем, что сам бот имеет право писать в этот чат.
    me = await bot.me()
    try:
        member = await bot.get_chat_member(message.chat.id, me.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Не удалось проверить права бота в чате %s: %s",
            message.chat.id,
            exc,
        )
        await message.answer("Не удалось проверить права бота в этом чате.")
        return

    if not _bot_can_post(member):
        await message.answer(
            "У бота нет прав на отправку сообщений в этом чате. "
            "Добавьте бота как участника (или администратора с правом "
            "отправлять сообщения) и повторите команду."
        )
        return

    created = await db.upsert_group(
        chat_id=message.chat.id,
        title=message.chat.title or "",
    )
    logger.info(
        "Группа %s (%s) %s рассылкой.",
        message.chat.id,
        message.chat.title,
        "добавлена к" if created else "реактивирована в",
    )
    await message.answer(
        "✅ Группа добавлена в список рассылки."
        if created
        else "♻️ Группа уже была в списке — снова активна."
    )


# =============================================================================
# /removegroup — в группе (удалить текущую) или в ЛС (с chat_id)
# =============================================================================


@router.message(Command("removegroup"))
async def cmd_removegroup(
    message: Message, command: CommandObject, db: Database
) -> None:
    if message.from_user is None or not settings.is_admin(message.from_user.id):
        return  # тихо игнорируем не-админов

    # Вариант 1: в ЛС администратор указывает chat_id явно.
    if message.chat.type == ChatType.PRIVATE:
        chat_id = _parse_int(command.args)
        if chat_id is None:
            await message.answer(
                "В приватном чате укажите chat_id: "
                "<code>/removegroup -1001234567890</code>\n"
                "Список групп можно получить через /listgroups."
            )
            return
    else:
        # Вариант 2: команда отправлена внутри самой группы.
        chat_id = message.chat.id

    ok = await db.remove_group(chat_id)
    if ok:
        await message.answer(
            f"🗑 Группа <code>{chat_id}</code> удалена из рассылки."
        )
        logger.info("Группа %s удалена из рассылки.", chat_id)
    else:
        await message.answer(
            f"Группа <code>{chat_id}</code> не найдена в списке."
        )


# =============================================================================
# /listgroups — список активных групп (в ЛС администратора)
# =============================================================================


@router.message(Command("listgroups"))
async def cmd_listgroups(message: Message, db: Database) -> None:
    if message.from_user is None or not settings.is_admin(message.from_user.id):
        return

    groups = await db.list_groups()
    if not groups:
        await message.answer(
            "Список групп пуст. В нужной группе выполните /addgroup."
        )
        return

    lines = ["<b>📣 Группы рассылки</b>"]
    for group in groups:
        status = "✅" if group.active else "⛔"
        title = html_escape(group.title or "(без названия)")
        lines.append(f"{status} <code>{group.chat_id}</code> — {title}")
    await message.answer("\n".join(lines))


# =============================================================================
# helpers
# =============================================================================


def _bot_can_post(member) -> bool:  # noqa: ANN001
    """True, если бот состоит в чате и может писать сообщения."""
    status = member.status
    if status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        return False
    if status == ChatMemberStatus.RESTRICTED:
        # ChatMemberRestricted даёт поле can_send_messages.
        return bool(getattr(member, "can_send_messages", False))
    if status == ChatMemberStatus.ADMINISTRATOR:
        # Если админ ограничен по отправке сообщений — учесть.
        can_post = getattr(member, "can_post_messages", None)
        if can_post is False:  # None → считаем что можно
            return False
    return True


def _parse_int(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None
