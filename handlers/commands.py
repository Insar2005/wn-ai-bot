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
    "Привет, я Джарвис — AI-ассистент официанта в Waiter Note.\n\n"
    "Я умею:\n"
    "• Отвечать на вопросы про работу, чаевые, гостей\n"
    "• Расшифровывать голосовые (пришли — переведу в текст)\n"
    "• Разбирать фото — чеки, меню, что угодно\n"
    "• Работать на русском, казахском, узбекском и других языках\n\n"
    "Скоро научусь сам добавлять напоминалки, столы, меню и открывать смены.\n\n"
    "Команды:\n"
    "/clear — очистить историю нашего разговора\n"
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
        f"Стёр {deleted} сообщений. Начнём с чистого листа."
        if deleted
        else "У нас и так пустая история — писать нечего."
    )
