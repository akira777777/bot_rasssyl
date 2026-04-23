"""
handlers/posts.py
=================
Команды управления постами (только для администраторов):

* /addpost [forward|copy]   — как реплай на сообщение, сохраняет его.
* /listposts                — список сохранённых постов.
* /removepost <id>          — удалить пост.
* /clearposts               — удалить все посты.
* /setcaption <id> <top|bottom|none> [текст...]  — подпись к посту.

Идея работы /addpost:
1. Администратор пересылает в приватный чат с ботом нужный контент
   (из канала, другого чата или набирает вручную).
2. Делает «Reply» на это сообщение с командой `/addpost` (или
   `/addpost forward`, если хочет сохранять «Переслано от»).
3. Бот запоминает (source_chat_id, message_id) этого сообщения и
   при рассылке либо forward_message его, либо copy_message.
"""

from __future__ import annotations

import logging
from html import escape as html_escape
from typing import Optional

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from database import Database
from utils import AdminFilter, detect_media_type


logger = logging.getLogger(__name__)

# Роутер — только для приватных чатов администраторов.
router = Router(name="posts")
router.message.filter(AdminFilter())


# =============================================================================
# /addpost
# =============================================================================


@router.message(Command("addpost"))
async def cmd_addpost(
    message: Message, command: CommandObject, db: Database
) -> None:
    """Сохранить сообщение, на которое это — реплай.

    Примеры:
        /addpost            (по умолчанию copy, без подписи)
        /addpost forward    (сохранить с «переслано от …»)
        /addpost copy       (явно указать copy)
    """
    replied = message.reply_to_message
    if replied is None:
        await message.answer(
            "⚠️ Пришлите нужное сообщение боту и ответьте на него "
            "командой <code>/addpost</code>.\n\n"
            "Можно указать режим:\n"
            "• <code>/addpost forward</code> — с источником\n"
            "• <code>/addpost copy</code> — без источника (по умолчанию)"
        )
        return

    mode = (command.args or "copy").strip().lower()
    if mode not in ("forward", "copy"):
        await message.answer(
            f"Неизвестный режим <code>{html_escape(mode)}</code>. "
            "Используйте <code>forward</code> или <code>copy</code>."
        )
        return

    media_type = detect_media_type(replied)

    post_id = await db.add_post(
        post_type=mode,
        source_chat_id=replied.chat.id,
        message_id=replied.message_id,
        media_type=media_type,
        added_by=message.from_user.id if message.from_user else 0,
    )

    logger.info(
        "Админ %s добавил пост #%d (%s, %s).",
        message.from_user.id if message.from_user else "?",
        post_id,
        mode,
        media_type,
    )

    await message.answer(
        f"✅ Пост сохранён под ID <b>#{post_id}</b>\n"
        f"Режим: <b>{mode}</b>\n"
        f"Тип: <b>{html_escape(media_type)}</b>\n\n"
        f"Подпись можно добавить: "
        f"<code>/setcaption {post_id} top Ваш текст</code>"
    )


# =============================================================================
# /listposts
# =============================================================================


@router.message(Command("listposts"))
async def cmd_listposts(message: Message, db: Database) -> None:
    posts = await db.list_posts()
    if not posts:
        await message.answer("Постов пока нет. Добавьте через /addpost.")
        return

    lines = ["<b>📋 Сохранённые посты</b>"]
    for post in posts:
        caption_mark = ""
        if post.caption_override:
            preview = html_escape((post.caption_override or "")[:30])
            caption_mark = (
                f" · подпись {post.caption_position or 'top'} «{preview}…»"
            )
        lines.append(
            f"• <code>#{post.id}</code> "
            f"[{post.post_type}/{html_escape(post.media_type)}]"
            f" src={post.source_chat_id} msg={post.message_id}"
            f"{caption_mark}"
        )
    await message.answer("\n".join(lines))


# =============================================================================
# /removepost
# =============================================================================


@router.message(Command("removepost"))
async def cmd_removepost(
    message: Message, command: CommandObject, db: Database
) -> None:
    post_id = _parse_int(command.args)
    if post_id is None:
        await message.answer(
            "Используйте <code>/removepost ID</code>, например "
            "<code>/removepost 3</code>."
        )
        return

    ok = await db.remove_post(post_id)
    if ok:
        await message.answer(f"🗑 Пост #{post_id} удалён.")
        logger.info(
            "Пост #%d удалён пользователем %s.",
            post_id,
            message.from_user.id if message.from_user else "?",
        )
    else:
        await message.answer(f"Пост #{post_id} не найден.")


# =============================================================================
# /clearposts
# =============================================================================


@router.message(Command("clearposts"))
async def cmd_clearposts(message: Message, db: Database) -> None:
    removed = await db.clear_posts()
    await message.answer(f"🧹 Удалено постов: <b>{removed}</b>.")
    logger.info(
        "Все посты очищены (%d шт.) пользователем %s.",
        removed,
        message.from_user.id if message.from_user else "?",
    )


# =============================================================================
# /setcaption
# =============================================================================


@router.message(Command("setcaption"))
async def cmd_setcaption(
    message: Message, command: CommandObject, db: Database
) -> None:
    """Синтаксис:
        /setcaption <id> top|bottom <текст ...>
        /setcaption <id> none            — снять подпись
    """
    if not command.args:
        await message.answer(
            "Формат: <code>/setcaption ID top|bottom текст</code> "
            "или <code>/setcaption ID none</code>."
        )
        return

    parts = command.args.split(maxsplit=2)
    if len(parts) < 2:
        await message.answer(
            "Формат: <code>/setcaption ID top|bottom текст</code> "
            "или <code>/setcaption ID none</code>."
        )
        return

    post_id = _parse_int(parts[0])
    position = parts[1].lower()
    text = parts[2] if len(parts) >= 3 else None

    if post_id is None:
        await message.answer("Не удалось распознать ID поста.")
        return
    if position not in ("top", "bottom", "none"):
        await message.answer("Позиция должна быть: top, bottom или none.")
        return

    post = await db.get_post(post_id)
    if post is None:
        await message.answer(f"Пост #{post_id} не найден.")
        return

    if position == "none":
        await db.set_post_caption(post_id, None, None)
        await message.answer(f"Подпись у поста #{post_id} снята.")
        return

    if not text:
        await message.answer("Укажите текст подписи после позиции.")
        return

    await db.set_post_caption(post_id, text, position)
    await message.answer(
        f"✅ Подпись у поста #{post_id} обновлена "
        f"(позиция: <b>{position}</b>)."
    )


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
