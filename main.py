"""
main.py
=======
Точка входа Telegram-бота.

Последовательность старта:
1. Загружаем .env (уже произошло в config.py при импорте).
2. Настраиваем логирование.
3. Открываем SQLite и создаём таблицы, если их нет.
4. Создаём Bot и Dispatcher (parse_mode = HTML по умолчанию).
5. Прокидываем зависимости в dispatcher.workflow_data — aiogram 3.x
   автоматически подставит их в хэндлеры по имени параметра.
6. Регистрируем роутеры, публикуем команды в меню Telegram.
7. Запускаем планировщик рассылки.
8. Запускаем long-polling.

На shutdown (Ctrl+C / SIGTERM):
* Останавливаем планировщик (wait=False).
* Закрываем сессию Bot и соединение с БД.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats

from broadcast import BroadcastService
from config import settings
from database import Database
from handlers import all_routers
from scheduler import BroadcastScheduler
from utils import setup_logging


logger = logging.getLogger(__name__)


# Список команд, который появится в меню-кнопке в приватном чате.
_PRIVATE_COMMANDS: list[BotCommand] = [
    BotCommand(command="help", description="Справка"),
    BotCommand(command="status", description="Статус бота"),
    BotCommand(command="addpost", description="Добавить пост (реплай)"),
    BotCommand(command="listposts", description="Список постов"),
    BotCommand(command="removepost", description="Удалить пост по ID"),
    BotCommand(command="clearposts", description="Удалить все посты"),
    BotCommand(command="setcaption", description="Подпись к посту"),
    BotCommand(command="listgroups", description="Список групп"),
    BotCommand(command="setdelay", description="Задержка (сек)"),
    BotCommand(command="setinterval", description="Интервал циклов"),
    BotCommand(command="setrotation", description="Режим ротации"),
    BotCommand(command="sendnow", description="Разослать сейчас"),
    BotCommand(command="id", description="Показать user_id / chat_id"),
]


async def _configure_bot_commands(bot: Bot) -> None:
    """Регистрируем меню команд в приватных чатах.

    В группах меню не публикуем — это снизит шум для рядовых
    участников. Админ-команды доступны только администраторам из
    белого списка в любом случае (проверка через AdminFilter).
    """
    try:
        await bot.set_my_commands(
            _PRIVATE_COMMANDS,
            scope=BotCommandScopeAllPrivateChats(),
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "Не удалось обновить список команд бота.", exc_info=True
        )


async def main() -> None:
    # 1. Логирование.
    setup_logging(settings.log_level)
    logger.info(
        "Старт бота. admin_ids=%s, tz=%s",
        settings.admin_ids,
        settings.timezone,
    )

    # 2. База данных.
    db = Database(settings.database_path)
    await db.init()

    # 3. Bot + Dispatcher. HTML parse_mode по умолчанию, но MarkdownV2
    #    можно использовать точечно через parse_mode= у send_message.
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    # 4. Сервисы.
    broadcast_service = BroadcastService(bot, db)
    broadcast_scheduler = BroadcastScheduler(
        broadcast_service, db, timezone_name=settings.timezone
    )

    # 5. Dependency injection: значения из workflow_data автоматически
    #    резолвятся в параметры хэндлеров по имени.
    dp["db"] = db
    dp["broadcast"] = broadcast_service
    dp["broadcast_scheduler"] = broadcast_scheduler

    # 6. Роутеры.
    dp.include_routers(*all_routers)

    # 7. Меню команд.
    await _configure_bot_commands(bot)

    # 8. Планировщик.
    await broadcast_scheduler.start()

    # 9. Long-polling. start_polling() сам перехватывает SIGINT/SIGTERM
    #    и корректно доит текущий апдейт; блок finally ниже гасит всё,
    #    что мы создали сами.
    try:
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        logger.info("Graceful shutdown...")
        await broadcast_scheduler.shutdown()
        await bot.session.close()
        await db.close()
        logger.info("Бот остановлен корректно.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        # asyncio.run корректно прокинет CancelledError в main(),
        # блок finally отработает — здесь просто не шумим стеком.
        logging.getLogger(__name__).info("Остановлено пользователем.")
