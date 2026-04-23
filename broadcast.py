"""
broadcast.py
============
Сервис рассылки поста по всем активным группам.

Основные гарантии:
* Один цикл рассылки одновременно. Защита через asyncio.Lock — если
  предыдущий цикл не завершился, следующий (от планировщика или /sendnow)
  пропускается с предупреждением в лог.
* Последовательная отправка с заданной задержкой между группами
  (min 1 секунда, по умолчанию 15).
* Внутри одной группы — жёсткий cooldown 5 минут: если бот уже писал
  туда менее 5 минут назад, пост пропускается.
* Аккуратная обработка Telegram-ошибок: FloodWait → ожидание + ретрай,
  Forbidden / «бот удалён» → автоматическая деактивация группы.
* Ротация: round-robin / random / single (один выбранный пост).
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter

from database import (
    Database,
    Group,
    Post,
    KEY_LAST_BROADCAST,
    KEY_LAST_INDEX,
    KEY_SINGLE_POST,
)
from utils import classify_send_error, parse_iso_utc


logger = logging.getLogger(__name__)

# Минимальный промежуток между отправками в одну и ту же группу.
PER_GROUP_COOLDOWN = timedelta(minutes=5)


@dataclass
class CycleStats:
    """Итоги одного цикла рассылки — возвращается /sendnow и в лог."""

    sent: int = 0
    skipped_cooldown: int = 0
    deactivated: int = 0
    failed: int = 0
    skipped_no_posts: bool = False
    skipped_no_groups: bool = False
    post_id: Optional[int] = None
    errors: List[str] = field(default_factory=list)

    def as_text(self) -> str:
        if self.skipped_no_posts:
            return "Рассылка пропущена: нет сохранённых постов."
        if self.skipped_no_groups:
            return "Рассылка пропущена: нет активных групп."
        lines = [
            f"Пост #{self.post_id} разослан.",
            f"• Отправлено: <b>{self.sent}</b>",
            f"• Пропущено по кулдауну 5 мин: <b>{self.skipped_cooldown}</b>",
            f"• Групп деактивировано: <b>{self.deactivated}</b>",
            f"• Ошибок: <b>{self.failed}</b>",
        ]
        if self.errors:
            shown = "\n".join(f"  – {e}" for e in self.errors[:5])
            lines.append("Примеры ошибок:\n" + shown)
        return "\n".join(lines)


class BroadcastService:
    """Инкапсулирует весь цикл рассылки.

    Экземпляр создаётся в main.py и передаётся:
    * в scheduler.py — для плановых запусков;
    * в handlers/admin.py — для команды /sendnow.
    """

    def __init__(self, bot: Bot, db: Database) -> None:
        self._bot = bot
        self._db = db
        # Лок гарантирует, что два цикла не идут параллельно.
        self._lock = asyncio.Lock()

    # ---------- публичные методы ----------

    async def run_cycle(
        self, *, post_id: Optional[int] = None, force: bool = False
    ) -> CycleStats:
        """Запустить один цикл рассылки.

        post_id — если указан, рассылается именно этот пост, игнорируя
                  режим ротации. Используется для /sendnow <id>.
        force   — параметр-заглушка на будущее; сейчас мы всегда
                  отказываемся от перекрывающих запусков, чтобы не
                  перегружать Telegram.
        """
        if self._lock.locked():
            logger.warning(
                "Пропущен цикл рассылки: предыдущий ещё не завершён."
            )
            stats = CycleStats()
            stats.errors.append("Предыдущий цикл ещё не завершён — пропуск.")
            return stats

        async with self._lock:
            return await self._run_locked(post_id=post_id)

    # ---------- приватные методы ----------

    async def _run_locked(self, *, post_id: Optional[int]) -> CycleStats:
        stats = CycleStats()
        post = await self._choose_post(explicit_post_id=post_id)
        if post is None:
            stats.skipped_no_posts = True
            logger.info("Нет постов для рассылки.")
            return stats
        stats.post_id = post.id

        groups = await self._db.list_groups(only_active=True)
        if not groups:
            stats.skipped_no_groups = True
            logger.info("Нет активных групп для рассылки.")
            return stats

        delay = await self._db.get_delay_seconds()
        logger.info(
            "Старт цикла: post_id=%s, групп=%d, delay=%d сек",
            post.id,
            len(groups),
            delay,
        )

        for i, group in enumerate(groups):
            # 1) Cooldown на группу.
            if self._is_on_cooldown(group):
                stats.skipped_cooldown += 1
                logger.info(
                    "chat_id=%s пропущен (cooldown 5 мин).", group.chat_id
                )
            else:
                ok = await self._send_to_group(group, post, stats)
                if ok:
                    stats.sent += 1
                    await self._db.mark_group_sent(group.chat_id)

            # 2) Задержка между группами (кроме последней).
            if i < len(groups) - 1:
                await asyncio.sleep(delay)

        await self._db.set_setting(
            KEY_LAST_BROADCAST,
            datetime.now(tz=timezone.utc).isoformat(),
        )
        logger.info(
            "Цикл завершён: post=%s sent=%d cooldown=%d deact=%d fail=%d",
            post.id,
            stats.sent,
            stats.skipped_cooldown,
            stats.deactivated,
            stats.failed,
        )
        return stats

    @staticmethod
    def _is_on_cooldown(group: Group) -> bool:
        last = parse_iso_utc(group.last_sent_at)
        if last is None:
            return False
        return (datetime.now(tz=timezone.utc) - last) < PER_GROUP_COOLDOWN

    async def _send_to_group(
        self, group: Group, post: Post, stats: CycleStats
    ) -> bool:
        """Отправка одного поста в одну группу с ретраем на FloodWait."""
        try:
            await self._send_once(group, post)
            return True
        except TelegramRetryAfter as exc:
            wait = int(exc.retry_after) + 1
            logger.warning(
                "FloodWait для chat_id=%s: ждём %d сек и повторяем.",
                group.chat_id,
                wait,
            )
            await asyncio.sleep(wait)
            try:
                await self._send_once(group, post)
                return True
            except Exception as exc2:  # noqa: BLE001
                return await self._handle_error(group, exc2, stats)
        except Exception as exc:  # noqa: BLE001
            return await self._handle_error(group, exc, stats)

    async def _handle_error(
        self, group: Group, error: BaseException, stats: CycleStats
    ) -> bool:
        """Единая точка разбора исключений при отправке."""
        category, should_deactivate = classify_send_error(error)
        logger.warning(
            "Ошибка отправки в chat_id=%s (%s): %s",
            group.chat_id,
            category,
            error,
        )
        stats.errors.append(f"{group.chat_id}: {category}: {error}")
        if should_deactivate:
            await self._db.deactivate_group(group.chat_id)
            stats.deactivated += 1
        else:
            stats.failed += 1
        return False

    async def _send_once(self, group: Group, post: Post) -> None:
        """Одна попытка доставки, без обработки ошибок.

        Порядок действий:
        1) Если caption_position == "top" — отдельным сообщением отправляем
           caption_override.
        2) Основная публикация — forward или copy.
        3) Если caption_position == "bottom" — отправляем подпись следом.

        Такой подход работает для ЛЮБЫХ типов сообщений (текст, медиа,
        стикер, опрос) без риска потерять оригинальное содержимое.
        """
        if post.caption_override and post.caption_position == "top":
            await self._bot.send_message(
                chat_id=group.chat_id,
                text=post.caption_override,
                disable_web_page_preview=True,
            )

        if post.post_type == "forward":
            # forward_message сохраняет цепочку «Переслано от …».
            # Если админ переслал пост из канала, Telegram покажет в
            # группе исходник канала — это искомое поведение.
            await self._bot.forward_message(
                chat_id=group.chat_id,
                from_chat_id=post.source_chat_id,
                message_id=post.message_id,
            )
        else:
            # copy_message публикует содержимое без указания источника.
            await self._bot.copy_message(
                chat_id=group.chat_id,
                from_chat_id=post.source_chat_id,
                message_id=post.message_id,
            )

        if post.caption_override and post.caption_position == "bottom":
            await self._bot.send_message(
                chat_id=group.chat_id,
                text=post.caption_override,
                disable_web_page_preview=True,
            )

    async def _choose_post(
        self, *, explicit_post_id: Optional[int]
    ) -> Optional[Post]:
        """Реализует три режима ротации.

        * explicit_post_id → берём именно его.
        * mode == "single" → берём пост из настройки single_post_id.
        * mode == "random" → равномерно случайно.
        * mode == "round"  → по кругу; индекс хранится в settings.
        """
        posts = await self._db.list_posts()
        if not posts:
            return None

        if explicit_post_id is not None:
            return next((p for p in posts if p.id == explicit_post_id), None)

        mode = await self._db.get_rotation_mode()

        if mode == "single":
            sid = await self._db.get_setting(KEY_SINGLE_POST)
            if sid is None:
                logger.warning(
                    "Режим single, но single_post_id не задан — беру первый."
                )
                return posts[0]
            try:
                target_id = int(sid)
            except ValueError:
                return posts[0]
            return next((p for p in posts if p.id == target_id), posts[0])

        if mode == "random":
            return random.choice(posts)

        # round-robin (по умолчанию)
        raw_idx = await self._db.get_setting(KEY_LAST_INDEX, "-1")
        try:
            last_idx = int(raw_idx or "-1")
        except ValueError:
            last_idx = -1
        next_idx = (last_idx + 1) % len(posts)
        await self._db.set_setting(KEY_LAST_INDEX, str(next_idx))
        return posts[next_idx]
