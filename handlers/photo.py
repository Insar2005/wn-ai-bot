"""Фото: скачиваем самую большую версию → Claude Haiku Vision → ответ."""
from __future__ import annotations

import logging

from aiogram import Router, F
from aiogram.enums import ChatAction
from aiogram.types import Message

from ai.claude import chat_vision
from config import settings
from db import load_recent_history, save_message

log = logging.getLogger(__name__)

router = Router()


@router.message(F.photo)
async def handle_photo(message: Message) -> None:
    if not message.from_user or not message.photo:
        return
    user_id = message.from_user.id

    # Telegram присылает несколько разрешений одного фото — берём
    # самое большое (последнее в массиве).
    photo = message.photo[-1]
    caption = (message.caption or "").strip() or "Фото без подписи."

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    # Скачиваем фото в память
    file = await message.bot.get_file(photo.file_id)
    if not file.file_path:
        await message.answer("Не смог скачать фото, попробуй ещё раз.")
        return

    from io import BytesIO
    buf = BytesIO()
    await message.bot.download_file(file.file_path, destination=buf)
    image_bytes = buf.getvalue()

    # Telegram отдаёт photos как JPEG почти всегда. Даже если это
    # был PNG — Telegram пережимает. Так что media_type всегда jpeg.
    media_type = "image/jpeg"

    history = await load_recent_history(
        user_id, limit=settings.context_messages_limit
    )

    try:
        reply, _ = await chat_vision(
            history=history,
            image_bytes=image_bytes,
            image_media_type=media_type,
            user_caption=caption,
        )
    except Exception:
        log.exception("Claude vision failed")
        await message.answer(
            "Не смог разобрать фото. Попробуй ещё раз или пришли другое."
        )
        return

    # В историю пишем ТОЛЬКО текстовую подпись — картинки не сохраняем
    # (тяжёлые, дорого будут стоить в контексте следующих запросов).
    # Пометка "[фото]" даст Claude понять что был визуальный контент.
    await save_message(
        user_id,
        "user",
        "photo",
        f"[фото] {caption}",
        metadata={"photo_file_id": photo.file_id},
    )
    await save_message(user_id, "assistant", "text", reply)

    await message.answer(reply)
