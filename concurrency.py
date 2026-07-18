"""Защита от параллельной обработки сообщений одного юзера + живой typing.

Проблемы, которые закрывает:
  1. Длинная задача (импорт меню, чистка категорий) идёт 30-60 сек;
     юзер пишет ещё раз → aiogram запускает ВТОРОЙ агент-цикл
     параллельно → две руки одновременно правят меню. Теперь второй
     запрос получает вежливый «ещё работаю» и не запускается.
  2. ChatAction.TYPING живёт ~5 секунд — на долгой задаче бот выглядит
     «пропавшим». typing() шлёт индикатор каждые 4.5 с, пока идёт
     работа.
"""
from __future__ import annotations

import asyncio
import contextlib

from aiogram.enums import ChatAction

BUSY_MSG = (
    "Секунду — ещё выполняю твой прошлый запрос. Закончу, отчитаюсь, "
    "и тогда продолжим."
)

_locks: dict[int, asyncio.Lock] = {}


def lock_for(tg_id: int) -> asyncio.Lock:
    return _locks.setdefault(tg_id, asyncio.Lock())


def is_busy(tg_id: int) -> bool:
    return lock_for(tg_id).locked()


@contextlib.asynccontextmanager
async def typing(bot, chat_id: int):
    """Держит индикатор «печатает…» живым, пока выполняется тело."""
    stop = asyncio.Event()

    async def _loop() -> None:
        while not stop.is_set():
            with contextlib.suppress(Exception):
                await bot.send_chat_action(chat_id, ChatAction.TYPING)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=4.5)

    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        stop.set()
        with contextlib.suppress(Exception):
            await task