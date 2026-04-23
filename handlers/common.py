"""
handlers/common.py
==================
Общие команды: /start, /help, /id, а также catch-all для неизвестных
команд. Этот роутер регистрируется ПОСЛЕДНИМ, чтобы не перехватывать
конкретные команды из posts/groups/admin.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from config import settings


router = Router(name="common")


_HELP_ADMIN = """\
<b>🤖 Бот-рассыльщик — справка администратора</b>

<b>📝 Управление постами (в приватном чате):</b>
• Пришлите боту сообщение → ответьте на него <code>/addpost</code> \
(или <code>/addpost forward</code>, чтобы рассылать с источником).
• <code>/listposts</code> — список сохранённых постов.
• <code>/removepost ID</code> — удалить конкретный.
• <code>/clearposts</code> — удалить все.
• <code>/setcaption ID top|bottom текст</code> — задать подпись сверху/снизу.
• <code>/setcaption ID none</code> — снять подпись.

<b>📣 Группы (внутри группы):</b>
• <code>/addgroup</code> — добавить текущую группу.
• <code>/removegroup</code> — убрать (или в ЛС: <code>/removegroup -100…</code>).
• <code>/listgroups</code> — список групп.

<b>🕒 Расписание и параметры:</b>
• <code>/setdelay N</code> — задержка между группами (сек). Рекоменд. 15–30.
• <code>/setinterval 4h</code> / <code>30m</code> / <code>1d</code> — интервал циклов.
• <code>/setrotation round|random|single [ID]</code> — режим ротации.
• <code>/sendnow [ID]</code> — запустить цикл прямо сейчас.
• <code>/status</code> — текущее состояние.

<b>🆔 Прочее:</b>
• <code>/id</code> — показать ваш user_id и chat_id.
"""


_HELP_GUEST = (
    "Это приватный бот-рассыльщик. Если вы не администратор, команды "
    "работать не будут. Обратитесь к владельцу бота, если нужен доступ."
)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if message.from_user and settings.is_admin(message.from_user.id):
        await message.answer(
            "Привет! Я готов к работе. Наберите /help, чтобы увидеть "
            "список команд."
        )
    else:
        await message.answer(_HELP_GUEST)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if message.from_user and settings.is_admin(message.from_user.id):
        await message.answer(_HELP_ADMIN)
    else:
        await message.answer(_HELP_GUEST)


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    """Маленький помощник: показывает user_id и chat_id — полезно при
    настройке (чтобы узнать собственный id или id группы).
    """
    user_id = message.from_user.id if message.from_user else "—"
    await message.answer(
        f"user_id: <code>{user_id}</code>\n"
        f"chat_id: <code>{message.chat.id}</code>\n"
        f"тип чата: <code>{message.chat.type}</code>"
    )


# ------------------------------------------------------------------
# Catch-all для неизвестных команд в приватном чате.
# Ограничиваем ChatType.PRIVATE — в группах мы не хотим реагировать на
# любые апдейты.
# ------------------------------------------------------------------
@router.message(F.chat.type == ChatType.PRIVATE, F.text.startswith("/"))
async def unknown_command(message: Message) -> None:
    if message.from_user and settings.is_admin(message.from_user.id):
        await message.answer(
            "Неизвестная команда. Наберите /help, чтобы увидеть список."
        )
    # Для не-администраторов — молчим, чтобы не раскрывать список команд.
