"""
handlers
========
Пакет обработчиков. Импортируется в main.py через `from handlers import all_routers`.

Порядок в списке важен: aiogram проходит роутеры сверху вниз, и первый
подходящий хэндлер поглощает апдейт. Поэтому:

1. posts_router, groups_router, admin_router — конкретные команды.
2. common_router — /start, /help и catch-all для неизвестных команд.
"""

from aiogram import Router

from .posts import router as posts_router
from .groups import router as groups_router
from .admin import router as admin_router
from .common import router as common_router


all_routers: list[Router] = [
    posts_router,
    groups_router,
    admin_router,
    common_router,
]

__all__ = ["all_routers"]
