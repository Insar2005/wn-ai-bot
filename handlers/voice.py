"""Голосовые сообщения: скачиваем OGG → Whisper → agentic chat → ответ."""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.types import Message

from ai.claude import chat_text
from ai.whisper import transcribe
from config import settings
from db import load_recent_history, save_message
from handlers.text import NOT_REGISTERED_MSG
from tools.impl import resolve_user

log = logging.getLogger(__name__)

router = Router()


@router.message(F.voice)
async def handle_voice(message: Message) -> None:
    if not message.from_user or not message.voice:
        return
    tg_id = message.from_user.id

    if message.voice.duration > settings.max_voice_seconds:
        await message.answer(
            f"Слишком длинное сообщение ({message.voice.duration} сек). "
            f"Максимум {settings.max_voice_seconds} секунд."
        )
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    user = await resolve_user(tg_id)
    if user is None:
        await message.answer(NOT_REGISTERED_MSG)
        return

    if user["is_disabled"]:
        await message.answer(
            "Твой аккаунт временно заблокирован. Проверь приложение."
        )
        return

    file = await message.bot.get_file(message.voice.file_id)
    if not file.file_path:
        await message.answer("Не смог скачать голосовое, попробуй ещё раз.")
        return

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        await message.bot.download_file(file.file_path, destination=tmp_path)

        try:
            transcript = await transcribe(tmp_path)
        except Exception:
            log.exception("Whisper failed")
            await message.answer(
                "Не смог разобрать голосовое. Попробуй ещё раз или "
                "напиши текстом."
            )
            return

        if not transcript.strip():
            await message.answer(
                "Голосовое пустое или неразборчивое. Попробуй ещё раз."
            )
            return

        await message.answer(f"🎙 «{transcript}»")

        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        history = await load_recent_history(
            tg_id, limit=settings.context_messages_limit
        )
        try:
            reply, _ = await chat_text(
                history=history,
                user_message=transcript,
                user_id=user["user_id"],
            )
        except Exception:
            log.exception("Claude call after voice failed")
            await message.answer(
                "Расслышал, но не смог ответить. Попробуй ещё раз."
            )
            return

        await save_message(
            tg_id,
            "user",
            "voice",
            transcript,
            metadata={"duration_sec": message.voice.duration},
        )
        await save_message(tg_id, "assistant", "text", reply)

        await message.answer(reply)

    finally:
        tmp_path.unlink(missing_ok=True)
