"""
utils.py
========
Мелкие утилиты общего назначения: настройка логирования, фильтр
администратора, определение типа медиа, форматирование времени и
классификация ошибок Telegram.
"""

from __future__ import annotations

import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from aiogram.filters import BaseFilter
from aiogram.types import Message

from config import settings


# =============================================================================
# Логирование
# =============================================================================


_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_LOG_FILE = "bot.log"


def setup_logging(level: str = "INFO") -> None:
    """Инициализирует корневой логгер.

    Логи пишутся одновременно в stdout и в ротируемый файл `bot.log`
    (5 МБ × 3 файла). Повторные вызовы безопасны — старые хендлеры
    удаляются, чтобы не дублировать строки.
    """
    root = logging.getLogger()
    root.setLevel(level.upper())

    # Удаляем прежние хендлеры (важно при горячем перезапуске).
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(_LOG_FORMAT)

    # Консольный вывод.
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)

    # Файловый вывод с ротацией (5 МБ × 3).
    file_handler = logging.handlers.RotatingFileHandler(
        _LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # aiogram/aiohttp по умолчанию многословны — успокаиваем.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("aiogram.dispatcher").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


# =============================================================================
# Фильтр администратора для aiogram-роутеров
# =============================================================================


class AdminFilter(BaseFilter):
    """Пропускает апдейты только от пользователей из белого списка.

    Использование (aiogram 3.x):
        router.message.filter(AdminFilter())
    либо на конкретном хэндлере:
        @router.message(Command("addpost"), AdminFilter())
    """

    async def __call__(self, message: Message) -> bool:
        user = message.from_user
        if user is None:
            return False
        return settings.is_admin(user.id)


# =============================================================================
# Определение типа медиа в сообщении
# =============================================================================


def detect_media_type(message: Message) -> str:
    """Возвращает строковый ярлык типа содержимого сообщения."""
    if message.photo:
        return "photo"
    if message.video:
        return "video"
    if message.animation:
        return "animation"
    if message.document:
        return "document"
    if message.audio:
        return "audio"
    if message.voice:
        return "voice"
    if message.video_note:
        return "video_note"
    if message.sticker:
        return "sticker"
    if message.poll:
        return "poll"
    if message.text:
        return "text"
    return "unknown"


# =============================================================================
# Форматирование времени
# =============================================================================


def parse_iso_utc(value: Optional[str]) -> Optional[datetime]:
    """Разбирает ISO-8601 строку (с таймзоной) в aware datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def format_eta(dt: Optional[datetime]) -> str:
    """Человекочитаемое представление будущего момента времени.

    Пример: «через 3 ч 42 мин (2026-04-23 10:42 UTC)».
    Если dt в прошлом или None — возвращает «нет данных» / «сейчас».
    """
    if dt is None:
        return "нет данных"
    now = datetime.now(tz=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = dt - now
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "сейчас"
    hours, remainder = divmod(seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours} ч")
    if minutes or not hours:
        parts.append(f"{minutes} мин")
    human = " ".join(parts)
    stamp = dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"через {human} ({stamp})"


# =============================================================================
# Классификация ошибок Telegram
# =============================================================================

# aiogram 3.x перенёс исключения в aiogram.exceptions.
from aiogram.exceptions import (  # noqa: E402
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)


# Паттерны в тексте TelegramBadRequest, означающие «группы больше нет»
# или «бот не может писать». При совпадении — помечаем группу неактивной.
_DEAD_CHAT_MARKERS = (
    "chat not found",
    "group chat was upgraded",
    "have no rights",
    "not enough rights",
    "bot was kicked",
    "bot is not a member",
    "chat_write_forbidden",
    "user is deactivated",
    "peer_id_invalid",
)


def classify_send_error(error: BaseException) -> Tuple[str, bool]:
    """Возвращает пару (категория, нужно_ли_деактивировать_группу).

    Категории:
        * "flood"      — нужно подождать error.retry_after секунд.
        * "forbidden"  — бот удалён/заблокирован в чате (403).
        * "bad_dead"   — BadRequest, но группа фактически недоступна.
        * "bad_other"  — BadRequest, прочие (например невалидный медиа).
        * "network"    — сетевая ошибка, временная.
        * "other"      — неизвестная ошибка.
    """
    if isinstance(error, TelegramRetryAfter):
        return "flood", False
    if isinstance(error, TelegramForbiddenError):
        return "forbidden", True
    if isinstance(error, TelegramBadRequest):
        msg = str(error).lower()
        if any(marker in msg for marker in _DEAD_CHAT_MARKERS):
            return "bad_dead", True
        return "bad_other", False
    if isinstance(error, TelegramNetworkError):
        return "network", False
    return "other", False


# =============================================================================
# Экранирование для MarkdownV2
# =============================================================================

_MDV2_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"


def escape_markdown_v2(text: str) -> str:
    """Экранирует текст для безопасной вставки в MarkdownV2.

    Основной parse_mode проекта — HTML, но бот поддерживает и MarkdownV2,
    поэтому функция экспортируется.
    """
    return "".join("\\" + ch if ch in _MDV2_SPECIAL else ch for ch in text)


# =============================================================================
# Путь до файла лога — для /status и отладки
# =============================================================================


def log_file_path() -> Path:
    return Path(_LOG_FILE).resolve()
