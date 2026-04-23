"""
database.py
===========
Асинхронный слой работы с SQLite (aiosqlite).

Таблицы:
* posts     — сохранённые посты для рассылки.
* groups    — группы, куда рассылаем.
* settings  — настройки (key/value): задержка, интервал, режим ротации, ...

Все даты хранятся как ISO-8601 строки в UTC.
Все методы класса Database — корутины; класс потокобезопасен в рамках
одного event loop (aiosqlite использует фоновый поток, а мы синхронизируем
доступ к соединению через внутренний lock, чтобы избежать race conditions
при одновременных операциях из разных обработчиков).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import aiosqlite


# -------- Доменные модели (read-only представления строк таблиц) --------


@dataclass(frozen=True)
class Post:
    id: int
    post_type: str  # 'forward' | 'copy'
    source_chat_id: int
    message_id: int
    caption_override: Optional[str]
    caption_position: Optional[str]  # 'top' | 'bottom' | None
    media_type: str
    added_at: str
    added_by: int


@dataclass(frozen=True)
class Group:
    chat_id: int
    title: str
    added_at: str
    active: bool
    last_sent_at: Optional[str]


# -------- Константы ключей settings --------

KEY_DELAY = "delay_seconds"
KEY_INTERVAL = "interval_minutes"
KEY_ROTATION = "rotation_mode"
KEY_SINGLE_POST = "single_post_id"
KEY_LAST_INDEX = "last_post_index"
KEY_LAST_BROADCAST = "last_broadcast_at"

DEFAULT_SETTINGS = {
    KEY_DELAY: "15",
    KEY_INTERVAL: "240",  # минут → 4 часа
    KEY_ROTATION: "round",
    KEY_LAST_INDEX: "-1",  # при старте первый next() даст 0
}

VALID_ROTATION = {"round", "random", "single"}


def _utcnow() -> str:
    """Единый формат временных меток — ISO-8601 UTC с таймзоной."""
    return datetime.now(tz=timezone.utc).isoformat()


class Database:
    """Тонкая обёртка над aiosqlite с доменными методами.

    Экземпляр создаётся в main.py через `Database(path)`, затем
    вызывается `await db.init()` для открытия соединения и миграций.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: Optional[aiosqlite.Connection] = None
        # Lock защищает от одновременных writes из разных обработчиков.
        self._lock = asyncio.Lock()

    # ------------------------ lifecycle ------------------------

    async def init(self) -> None:
        """Открывает соединение и создаёт таблицы при первом запуске."""
        self._conn = await aiosqlite.connect(self._path)
        # row_factory даёт доступ по именам колонок — удобно для моделей.
        self._conn.row_factory = aiosqlite.Row
        # WAL повышает конкурентность чтения/записи.
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        await self._create_tables()
        await self._apply_default_settings()
        await self._conn.commit()

    async def close(self) -> None:
        """Закрыть соединение при graceful shutdown."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _create_tables(self) -> None:
        assert self._conn is not None
        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                post_type        TEXT NOT NULL CHECK (post_type IN ('forward','copy')),
                source_chat_id   INTEGER NOT NULL,
                message_id       INTEGER NOT NULL,
                caption_override TEXT,
                caption_position TEXT CHECK (caption_position IN ('top','bottom') OR caption_position IS NULL),
                media_type       TEXT NOT NULL DEFAULT 'text',
                added_at         TEXT NOT NULL,
                added_by         INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS groups (
                chat_id      INTEGER PRIMARY KEY,
                title        TEXT NOT NULL DEFAULT '',
                added_at     TEXT NOT NULL,
                active       INTEGER NOT NULL DEFAULT 1,
                last_sent_at TEXT
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_groups_active ON groups(active);
            """
        )

    async def _apply_default_settings(self) -> None:
        """Вставляет значения по умолчанию для отсутствующих ключей."""
        assert self._conn is not None
        for key, value in DEFAULT_SETTINGS.items():
            await self._conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (key, value),
            )

    # ------------------------ posts ------------------------

    async def add_post(
        self,
        *,
        post_type: str,
        source_chat_id: int,
        message_id: int,
        media_type: str,
        added_by: int,
        caption_override: Optional[str] = None,
        caption_position: Optional[str] = None,
    ) -> int:
        """Сохраняет пост и возвращает присвоенный id."""
        if post_type not in ("forward", "copy"):
            raise ValueError(f"Некорректный post_type: {post_type}")
        if caption_position not in (None, "top", "bottom"):
            raise ValueError(
                f"Некорректный caption_position: {caption_position}"
            )
        async with self._lock:
            assert self._conn is not None
            cursor = await self._conn.execute(
                """
                INSERT INTO posts(post_type, source_chat_id, message_id,
                                  caption_override, caption_position,
                                  media_type, added_at, added_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    post_type,
                    source_chat_id,
                    message_id,
                    caption_override,
                    caption_position,
                    media_type,
                    _utcnow(),
                    added_by,
                ),
            )
            await self._conn.commit()
            return cursor.lastrowid or 0

    async def list_posts(self) -> List[Post]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM posts ORDER BY id ASC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_post(row) for row in rows]

    async def get_post(self, post_id: int) -> Optional[Post]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM posts WHERE id = ?", (post_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return self._row_to_post(row) if row else None

    async def remove_post(self, post_id: int) -> bool:
        """True, если запись действительно удалена."""
        async with self._lock:
            assert self._conn is not None
            cursor = await self._conn.execute(
                "DELETE FROM posts WHERE id = ?", (post_id,)
            )
            await self._conn.commit()
            return cursor.rowcount > 0

    async def clear_posts(self) -> int:
        """Удаляет все посты, возвращает сколько было удалено."""
        async with self._lock:
            assert self._conn is not None
            cursor = await self._conn.execute("DELETE FROM posts")
            await self._conn.commit()
        # Сбрасываем индекс round-robin — иначе он указывает в пустоту.
        await self.set_setting(KEY_LAST_INDEX, "-1")
        return cursor.rowcount or 0

    async def set_post_caption(
        self, post_id: int, text: Optional[str], position: Optional[str]
    ) -> bool:
        if position not in (None, "top", "bottom"):
            raise ValueError(f"Некорректный position: {position}")
        async with self._lock:
            assert self._conn is not None
            cursor = await self._conn.execute(
                "UPDATE posts SET caption_override = ?, caption_position = ? WHERE id = ?",
                (text, position, post_id),
            )
            await self._conn.commit()
            return cursor.rowcount > 0

    @staticmethod
    def _row_to_post(row: aiosqlite.Row) -> Post:
        return Post(
            id=row["id"],
            post_type=row["post_type"],
            source_chat_id=row["source_chat_id"],
            message_id=row["message_id"],
            caption_override=row["caption_override"],
            caption_position=row["caption_position"],
            media_type=row["media_type"],
            added_at=row["added_at"],
            added_by=row["added_by"],
        )

    # ------------------------ groups ------------------------

    async def upsert_group(self, chat_id: int, title: str) -> bool:
        """Добавляет группу или реактивирует существующую.

        Возвращает True, если группа новая (добавлена впервые).
        """
        async with self._lock:
            assert self._conn is not None
            async with self._conn.execute(
                "SELECT chat_id FROM groups WHERE chat_id = ?", (chat_id,)
            ) as cursor:
                existed = await cursor.fetchone() is not None
            if existed:
                await self._conn.execute(
                    "UPDATE groups SET title = ?, active = 1 WHERE chat_id = ?",
                    (title, chat_id),
                )
            else:
                await self._conn.execute(
                    """
                    INSERT INTO groups(chat_id, title, added_at, active)
                    VALUES (?, ?, ?, 1)
                    """,
                    (chat_id, title, _utcnow()),
                )
            await self._conn.commit()
            return not existed

    async def remove_group(self, chat_id: int) -> bool:
        async with self._lock:
            assert self._conn is not None
            cursor = await self._conn.execute(
                "DELETE FROM groups WHERE chat_id = ?", (chat_id,)
            )
            await self._conn.commit()
            return cursor.rowcount > 0

    async def deactivate_group(self, chat_id: int) -> None:
        """Помечает группу неактивной (бот потерял доступ)."""
        async with self._lock:
            assert self._conn is not None
            await self._conn.execute(
                "UPDATE groups SET active = 0 WHERE chat_id = ?", (chat_id,)
            )
            await self._conn.commit()

    async def mark_group_sent(self, chat_id: int) -> None:
        async with self._lock:
            assert self._conn is not None
            await self._conn.execute(
                "UPDATE groups SET last_sent_at = ? WHERE chat_id = ?",
                (_utcnow(), chat_id),
            )
            await self._conn.commit()

    async def list_groups(self, *, only_active: bool = False) -> List[Group]:
        assert self._conn is not None
        sql = "SELECT * FROM groups"
        if only_active:
            sql += " WHERE active = 1"
        sql += " ORDER BY added_at ASC"
        async with self._conn.execute(sql) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_group(row) for row in rows]

    @staticmethod
    def _row_to_group(row: aiosqlite.Row) -> Group:
        return Group(
            chat_id=row["chat_id"],
            title=row["title"],
            added_at=row["added_at"],
            active=bool(row["active"]),
            last_sent_at=row["last_sent_at"],
        )

    # ------------------------ settings (kv) ------------------------

    async def get_setting(
        self, key: str, default: Optional[str] = None
    ) -> Optional[str]:
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return default
        return row["value"]

    async def set_setting(self, key: str, value: Optional[str]) -> None:
        async with self._lock:
            assert self._conn is not None
            await self._conn.execute(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            await self._conn.commit()

    # ------------------------ удобные helpers ------------------------

    async def get_delay_seconds(self) -> int:
        raw = await self.get_setting(KEY_DELAY, DEFAULT_SETTINGS[KEY_DELAY])
        return max(1, int(raw or "15"))

    async def get_interval_minutes(self) -> int:
        raw = await self.get_setting(
            KEY_INTERVAL, DEFAULT_SETTINGS[KEY_INTERVAL]
        )
        return max(1, int(raw or "240"))

    async def get_rotation_mode(self) -> str:
        raw = await self.get_setting(
            KEY_ROTATION, DEFAULT_SETTINGS[KEY_ROTATION]
        )
        if raw not in VALID_ROTATION:
            return "round"
        return raw
