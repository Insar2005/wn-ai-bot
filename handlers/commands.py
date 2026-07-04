"""Команды бота: /start, /help, /clear."""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from db import clear_history

log = logging.getLogger(__name__)

router = Router()


WELCOME = (
    "Кибер Шеф на связи 👨‍🍳\n\n"
    "Я старший в этой смене — 20 лет у плиты и в зале, теперь работаю "
    "из облака. Пиши, спрашивай что угодно: как посчитать чаевые, "
    "что ответить занудному гостю, как раскидать счёт по компании, "
    "что за блюдо на фотке — разберёмся.\n\n"
    "Что умею:\n"
    "• Отвечать текстом, голосом, на фото\n"
    "• Работать на русском, казахском, узбекском и других\n"
    "• Считать в уме — чаевые, доли, скидки\n\n"
    "Смены открывать и меню менять из чата пока не могу — только через "
    "приложение. Но скоро руки развяжут.\n\n"
    "Команды:\n"
    "/clear — стереть наш разговор\n"
    "/help — эта справка"
)


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    await message.answer(WELCOME)


@router.message(Command("help"))
async def handle_help(message: Message) -> None:
    await message.answer(WELCOME)


@router.message(Command("clear"))
async def handle_clear(message: Message) -> None:
    if not message.from_user:
        return
    deleted = await clear_history(message.from_user.id)
    await message.answer(
        f"Стёр {deleted} сообщений. Погнали заново."
        if deleted
        else "А у нас и так пусто, стирать нечего."
    )