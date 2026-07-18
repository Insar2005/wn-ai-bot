"""Точка входа бота Джарвиса. Long polling (не webhook — проще на Railway).

Запуск локально:
    python main.py

Запуск на Railway:
    просто задеплой, procfile/railway.toml запустит main.py
"""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import settings
from db import close_pool, init_pool

# ── Sentry: только ошибки; включается наличием SENTRY_DSN в env ──────
import os

try:
    import sentry_sdk

    if os.getenv("SENTRY_DSN"):
        sentry_sdk.init(
            dsn=os.environ["SENTRY_DSN"],
            environment=os.getenv("RAILWAY_ENVIRONMENT_NAME", "production"),
            traces_sample_rate=0.0,
        )
except ImportError:
    pass
from handlers import router as main_router


def _setup_logging() -> None:
    logging.basicConfig(
        stream=sys.stdout,
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # aiogram сама по себе многословна на DEBUG, но на INFO норм
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def _main() -> None:
    _setup_logging()
    log = logging.getLogger("main")

    log.info("Starting Waiter Note AI bot (Jarvis)")
    log.info("Claude model: %s", settings.claude_model)
    log.info("Whisper model: %s", settings.whisper_model)

    await init_pool()

    bot = Bot(
        token=settings.telegram_bot_token,
        # HTML parse mode — на будущее (жирный/ссылки в ответах).
        # Пока Claude отдаёт plain text, но пусть будет.
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(main_router)

    try:
        # Убираем возможные webhook-настройки от прошлых запусков —
        # иначе polling не будет получать updates.
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Polling started")
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await close_pool()
        log.info("Bot stopped")


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except (KeyboardInterrupt, SystemExit):
        pass