"""Фото и изображения-документы → Claude Vision (с tools) → ответ.

Два входа:
  • F.photo — обычные фото из галереи (Telegram сам жмёт в JPEG).
  • F.document с image/* — файлы «как документ», включая HEIC с айфонов
    (частый случай: владелец пересылает фото меню файлом). HEIC/HEIF
    Claude API не принимает — конвертируем в JPEG через pillow-heif.
"""
from __future__ import annotations

import logging
from io import BytesIO

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.types import Message

from ai.claude import chat_vision
from concurrency import BUSY_MSG, is_busy, lock_for, typing
from config import settings
from db import load_recent_history, save_message
from handlers.text import NOT_REGISTERED_MSG
from tools.impl import get_active_workplace_title, resolve_user

log = logging.getLogger(__name__)

router = Router()

MAX_IMAGE_BYTES = 18 * 1024 * 1024  # лимит Telegram Bot API на download
CLAUDE_MEDIA = {"image/jpeg", "image/png", "image/webp", "image/gif"}
HEIC_MIME = {"image/heic", "image/heif"}
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif")


def _heic_to_jpeg(data: bytes) -> bytes:
    """HEIC/HEIF → JPEG. Бросает ImportError, если pillow-heif не стоит."""
    from pillow_heif import register_heif_opener  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    register_heif_opener()
    img = Image.open(BytesIO(data))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    out = BytesIO()
    img.save(out, format="JPEG", quality=88)
    return out.getvalue()


async def _vision_flow(
    message: Message,
    *,
    image_bytes: bytes,
    media_type: str,
    caption: str,
    meta: dict,
) -> None:
    """Общий путь: юзер → история → Claude Vision → сохранение → ответ."""
    tg_id = message.from_user.id

    if is_busy(tg_id):
        await message.answer(BUSY_MSG)
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

    history = await load_recent_history(
        tg_id, limit=settings.context_messages_limit
    )

    wp_title = await get_active_workplace_title(user["user_id"])
    wp_mark = (
        f"⟦заведение: {wp_title}⟧" if wp_title else "⟦заведение не выбрано⟧"
    )
    ctx_caption = f"{wp_mark} {caption}"

    try:
        async with lock_for(tg_id), typing(message.bot, message.chat.id):
            reply, _ = await chat_vision(
                history=history,
                image_bytes=image_bytes,
                image_media_type=media_type,
                user_caption=ctx_caption,
                user_id=user["user_id"],
            )
    except Exception:
        log.exception("Claude vision failed")
        await message.answer(
            "Не смог разобрать фото. Попробуй ещё раз или пришли другое."
        )
        return

    # В истории — только текстовое упоминание фото. Сами картинки не
    # сохраняем — они бы удвоили cost каждого следующего запроса.
    await save_message(tg_id, "user", "photo", f"[фото] {ctx_caption}", metadata=meta)
    await save_message(tg_id, "assistant", "text", reply)

    await message.answer(reply or "Готово.")


@router.message(F.photo)
async def handle_photo(message: Message) -> None:
    if not message.from_user or not message.photo:
        return

    photo = message.photo[-1]
    caption = (message.caption or "").strip() or "Фото без подписи."

    file = await message.bot.get_file(photo.file_id)
    if not file.file_path:
        await message.answer("Не смог скачать фото, попробуй ещё раз.")
        return

    buf = BytesIO()
    await message.bot.download_file(file.file_path, destination=buf)

    await _vision_flow(
        message,
        image_bytes=buf.getvalue(),
        media_type="image/jpeg",
        caption=caption,
        meta={"photo_file_id": photo.file_id},
    )


@router.message(F.document)
async def handle_image_document(message: Message) -> None:
    """Изображение, отправленное файлом (в т.ч. HEIC с iPhone)."""
    doc = message.document
    if not message.from_user or doc is None:
        return

    mime = (doc.mime_type or "").lower()
    name = (doc.file_name or "").lower()
    is_image = mime.startswith("image/") or name.endswith(IMAGE_EXT)
    if not is_image:
        await message.answer(
            "Файлы такого типа пока не читаю — пришли изображение "
            "(jpg, png, webp, heic) или обычное фото."
        )
        return

    if doc.file_size and doc.file_size > MAX_IMAGE_BYTES:
        await message.answer(
            "Файл слишком большой. Пришли фото полегче — можно обычным "
            "фото, а не файлом."
        )
        return

    caption = (message.caption or "").strip() or "Фото без подписи."

    file = await message.bot.get_file(doc.file_id)
    if not file.file_path:
        await message.answer("Не смог скачать файл, попробуй ещё раз.")
        return

    buf = BytesIO()
    await message.bot.download_file(file.file_path, destination=buf)
    image_bytes = buf.getvalue()

    if mime in HEIC_MIME or name.endswith((".heic", ".heif")):
        try:
            image_bytes = _heic_to_jpeg(image_bytes)
            media_type = "image/jpeg"
        except ImportError:
            log.error("pillow-heif is not installed")
            await message.answer(
                "HEIC пока не могу открыть на сервере. Пришли это же "
                "обычным фото (не файлом) — Telegram сам сконвертирует."
            )
            return
        except Exception:
            log.exception("HEIC convert failed")
            await message.answer(
                "Не смог открыть HEIC. Пришли обычным фото, не файлом."
            )
            return
    elif mime in CLAUDE_MEDIA:
        media_type = mime
    elif name.endswith((".png",)):
        media_type = "image/png"
    elif name.endswith((".webp",)):
        media_type = "image/webp"
    elif name.endswith((".gif",)):
        media_type = "image/gif"
    else:
        media_type = "image/jpeg"

    await _vision_flow(
        message,
        image_bytes=image_bytes,
        media_type=media_type,
        caption=caption,
        meta={"document_file_id": doc.file_id, "mime": mime or name},
    )