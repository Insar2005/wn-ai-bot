"""Все handlers в одном router-агрегаторе."""
from aiogram import Router

from handlers import commands, photo, text, voice

router = Router()
# ВАЖНО: порядок имеет значение. Команды идут первыми чтобы /start
# не попал в text-handler как обычное сообщение.
router.include_router(commands.router)
router.include_router(voice.router)
router.include_router(photo.router)
router.include_router(text.router)
