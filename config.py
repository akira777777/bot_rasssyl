"""
config.py
=========
Загрузка и валидация конфигурации из .env файла.

Экспортирует единственный объект `settings`, который используется всеми
остальными модулями проекта (main, database, scheduler, handlers, utils).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv

# Загружаем переменные окружения из .env (ищем в текущей директории)
load_dotenv()


def _parse_admin_ids(raw: str | None) -> List[int]:
    """Разбирает строку вида "111,222,333" в список int.

    Пустые элементы и пробелы игнорируются. Неверные значения
    вызовут ValueError на старте — это намеренно, чтобы не запускать
    бота с некорректным белым списком.
    """
    if not raw:
        return []
    result: List[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError as exc:
            raise ValueError(
                f"ADMIN_IDS содержит некорректный user_id: {part!r}"
            ) from exc
    return result


@dataclass(frozen=True)
class Settings:
    """Неизменяемый контейнер настроек приложения."""

    # Токен Telegram-бота, обязателен.
    bot_token: str
    # Белый список администраторов (user_id).
    admin_ids: List[int] = field(default_factory=list)
    # Путь к SQLite-базе данных.
    database_path: str = "bot.db"
    # Уровень логирования (DEBUG / INFO / WARNING / ERROR).
    log_level: str = "INFO"
    # Часовой пояс планировщика (IANA).
    timezone: str = "UTC"

    def is_admin(self, user_id: int) -> bool:
        """Проверка членства в белом списке администраторов."""
        return user_id in self.admin_ids


def _load_settings() -> Settings:
    """Читает .env и возвращает готовый Settings.

    Падает с понятным сообщением, если отсутствует BOT_TOKEN или
    admin_ids содержит мусор — это лучше, чем стартовать и падать
    позже в непредсказуемом месте.
    """
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError(
            "BOT_TOKEN не задан. Скопируйте .env.example в .env "
            "и укажите токен от @BotFather."
        )

    admin_ids = _parse_admin_ids(os.getenv("ADMIN_IDS"))
    if not admin_ids:
        # Без администраторов бот бесполезен — сразу сообщаем.
        raise RuntimeError(
            "ADMIN_IDS пуст. Укажите хотя бы один user_id администратора."
        )

    database_path = os.getenv("DATABASE_PATH", "bot.db").strip() or "bot.db"
    log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    timezone = os.getenv("TIMEZONE", "UTC").strip() or "UTC"

    # Приводим путь к абсолютному, чтобы рабочая директория
    # при запуске не влияла на местоположение БД.
    db_path = Path(database_path)
    if not db_path.is_absolute():
        db_path = Path.cwd() / db_path

    return Settings(
        bot_token=bot_token,
        admin_ids=admin_ids,
        database_path=str(db_path),
        log_level=log_level,
        timezone=timezone,
    )


# Глобальный экземпляр — используется всеми модулями.
settings: Settings = _load_settings()
