"""Обработка текстовых сообщений с function calling."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.types import Message

from ai.claude import chat_text
from concurrency import BUSY_MSG, is_busy, lock_for, typing
from config import settings
from db import load_recent_history, save_message
from tools.impl import resolve_user

log = logging.getLogger(__name__)

router = Router()


NOT_REGISTERED_MSG = (
    "Слушай, ты ещё не заходил в приложение Waiter Note — тебя в базе "
    "нет. Открой сначала приложение через нашего бота, залогинься и "
    "тогда я смогу смотреть твои смены, столы и всё остальное. "
    "Пока могу только болтать."
)


@router.message(F.text)
async def handle_text(message: Message) -> None:
    if not message.from_user or not message.text:
        return
    tg_id = message.from_user.id
    text = message.text.strip()
    if not text:
        return

    # Уже выполняем прошлый запрос этого юзера — не запускаем второй
    # агент-цикл параллельно (две руки в одном меню = каша).
    if is_busy(tg_id):
        await message.answer(BUSY_MSG)
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    # Резолвим tg_id → user_id
    user = await resolve_user(tg_id)
    if user is None:
        await message.answer(NOT_REGISTERED_MSG)
        return

    if user["is_disabled"]:
        await message.answer(
            "Твой аккаунт временно заблокирован. Проверь приложение."
        )
        return

    history = await load_recent_history(
        tg_id, limit=settings.context_messages_limit
    )

    try:
        # Лок на юзера + «печатает…» живёт всю долгую операцию.
        async with lock_for(tg_id), typing(message.bot, message.chat.id):
            reply, _usage = await chat_text(
                history=history,
                user_message=text,
                user_id=user["user_id"],
            )
    except Exception:
        log.exception("Claude text call failed")
        await message.answer(
            "Что-то у меня заклинило. Попробуй ещё раз через минуту."
        )
        return

    await save_message(tg_id, "user", "text", text)
    await save_message(tg_id, "assistant", "text", reply)

    await message.answer(reply or "Готово.")