"""Фото: скачиваем самую большую версию → Claude Vision (с tools) → ответ."""
from __future__ import annotations

import logging
from io import BytesIO

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.types import Message

from ai.claude import chat_vision
from config import settings
from db import load_recent_history, save_message
from handlers.text import NOT_REGISTERED_MSG
from tools.impl import resolve_user

log = logging.getLogger(__name__)

router = Router()


@router.message(F.photo)
async def handle_photo(message: Message) -> None:
    if not message.from_user or not message.photo:
        return
    tg_id = message.from_user.id

    photo = message.photo[-1]
    caption = (message.caption or "").strip() or "Фото без подписи."

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

    file = await message.bot.get_file(photo.file_id)
    if not file.file_path:
        await message.answer("Не смог скачать фото, попробуй ещё раз.")
        return

    buf = BytesIO()
    await message.bot.download_file(file.file_path, destination=buf)
    image_bytes = buf.getvalue()
    media_type = "image/jpeg"

    history = await load_recent_history(
        tg_id, limit=settings.context_messages_limit
    )

    try:
        reply, _ = await chat_vision(
            history=history,
            image_bytes=image_bytes,
            image_media_type=media_type,
            user_caption=caption,
            user_id=user["user_id"],
        )
    except Exception:
        log.exception("Claude vision failed")
        await message.answer(
            "Не смог разобрать фото. Попробуй ещё раз или пришли другое."
        )
        return

    # В истории — только текстовое упоминание фото. Сами картинки не
    # сохраняем в contextsave — они бы удвоили cost каждого следующего
    # запроса.
    await save_message(
        tg_id,
        "user",
        "photo",
        f"[фото] {caption}",
        metadata={"photo_file_id": photo.file_id},
    )
    await save_message(tg_id, "assistant", "text", reply)

    await message.answer(reply)
