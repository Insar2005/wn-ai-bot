"""Обработка обычных текстовых сообщений."""
from __future__ import annotations

import logging

from aiogram import Router, F
from aiogram.enums import ChatAction
from aiogram.types import Message

from ai.claude import chat_text
from config import settings
from db import load_recent_history, save_message

log = logging.getLogger(__name__)

router = Router()


# F.text без команды (команды перехватят commands.router выше)
@router.message(F.text)
async def handle_text(message: Message) -> None:
    if not message.from_user or not message.text:
        return
    user_id = message.from_user.id
    text = message.text.strip()
    if not text:
        return

    # "Typing…" индикатор пока думаем
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    # Загружаем контекст диалога
    history = await load_recent_history(
        user_id, limit=settings.context_messages_limit
    )

    # Спрашиваем Claude
    try:
        reply, _usage = await chat_text(history, text)
    except Exception as e:
        log.exception("Claude text call failed")
        await message.answer(
            "Что-то пошло не так с моей стороны. Попробуй ещё раз через минуту."
        )
        return

    # Сохраняем и юзерский запрос, и ответ Джарвиса
    await save_message(user_id, "user", "text", text)
    await save_message(user_id, "assistant", "text", reply)

    await message.answer(reply)
