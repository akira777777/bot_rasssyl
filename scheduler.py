"""
scheduler.py
============
Обёртка над APScheduler (AsyncIOScheduler) для периодической рассылки.

Предоставляет единственный класс `BroadcastScheduler`, который:
* Создаёт job «broadcast_cycle» с IntervalTrigger.
* Умеет перепланировать job без перезапуска процесса (/setinterval).
* Отдаёт время следующего запуска для /status.
* Корректно останавливается при graceful shutdown.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from pytz import timezone as pytz_timezone
from pytz import UnknownTimeZoneError

from broadcast import BroadcastService
from database import Database, KEY_INTERVAL


logger = logging.getLogger(__name__)

_JOB_ID = "broadcast_cycle"


class BroadcastScheduler:
    def __init__(
        self,
        broadcast: BroadcastService,
        db: Database,
        timezone_name: str = "UTC",
    ) -> None:
        self._broadcast = broadcast
        self._db = db
        self._tz = self._resolve_timezone(timezone_name)
        self._scheduler = AsyncIOScheduler(timezone=self._tz)

    @staticmethod
    def _resolve_timezone(name: str):
        try:
            return pytz_timezone(name)
        except UnknownTimeZoneError:
            logger.warning("Неизвестный TIMEZONE=%s — использую UTC.", name)
            return pytz_timezone("UTC")

    async def start(self) -> None:
        """Читает интервал из БД и запускает job."""
        minutes = await self._db.get_interval_minutes()
        self._scheduler.add_job(
            self._tick,
            trigger=IntervalTrigger(minutes=minutes),
            id=_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,           # схлопываем накопившиеся запуски в один
            misfire_grace_time=300,  # 5 минут терпимости к опозданиям
        )
        self._scheduler.start()
        logger.info(
            "Планировщик запущен: цикл каждые %d мин, TZ=%s",
            minutes,
            self._tz,
        )

    async def shutdown(self) -> None:
        """Корректно гасит планировщик (wait=False — не ждём текущую джобу)."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Планировщик остановлен.")

    async def reschedule(self, minutes: int) -> None:
        """Меняет интервал на лету и сохраняет его в БД."""
        if minutes < 1:
            raise ValueError("Интервал должен быть >= 1 минуты.")
        self._scheduler.reschedule_job(
            _JOB_ID, trigger=IntervalTrigger(minutes=minutes)
        )
        await self._db.set_setting(KEY_INTERVAL, str(minutes))
        logger.info("Интервал рассылки изменён на %d мин.", minutes)

    def next_run_time(self) -> Optional[datetime]:
        """Возвращает время следующего запуска job (aware datetime)."""
        job = self._scheduler.get_job(_JOB_ID)
        if job is None:
            return None
        return job.next_run_time

    async def _tick(self) -> None:
        """Обёртка-таск для планировщика.

        Ловит все исключения — иначе APScheduler отключит job после
        первой же ошибки.
        """
        try:
            stats = await self._broadcast.run_cycle()
            logger.info(
                "Плановый цикл завершён: %s",
                stats.as_text().replace("\n", " | "),
            )
        except Exception:  # noqa: BLE001
            logger.exception("Необработанное исключение в плановом цикле.")
