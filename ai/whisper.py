"""Транскрипция голосовых сообщений через Groq Whisper large-v3.

Groq — самый дешёвый hosted Whisper на рынке (~$0.02/hour), тот же
large-v3 что и у OpenAI, но на LPU-железе (быстрее и в 9x дешевле).

Whisper поддерживает 99 языков включая большинство CIS
(русский, казахский, узбекский, азербайджанский, армянский,
таджикский, туркменский, татарский, башкирский). Кыргызский
официально НЕ поддерживается — модель попытается распознать
как казахский/татарский, качество будет низким.

Параметр `language` мы НЕ задаём принудительно — пусть Whisper
сам определит по аудио. Это лучше для мультиязычной аудитории
(один и тот же официант может дать команду на русском, потом
на казахском, потом обратно).
"""
from __future__ import annotations

import logging
from pathlib import Path

from groq import AsyncGroq
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import settings

log = logging.getLogger(__name__)

_client = AsyncGroq(api_key=settings.groq_api_key)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
async def transcribe(audio_path: Path, prompt: str | None = None) -> str:
    """Отправить аудиофайл в Groq Whisper, вернуть распознанный текст.

    Args:
        audio_path: путь к OGG/MP3/WAV/M4A/WEBM файлу
        prompt: подсказка для модели (например, список названий блюд
                из меню заведения). Сильно поднимает точность на
                специфической лексике.

    Raises:
        Exception: если после 3 ретраев Groq так и не ответил.
    """
    log.info("Whisper: transcribing %s (%d bytes)",
             audio_path.name, audio_path.stat().st_size)
    with audio_path.open("rb") as f:
        # Синхронный open — OK, файлы маленькие (< 5MB для голосовых).
        # Сам запрос уходит асинхронно.
        response = await _client.audio.transcriptions.create(
            file=(audio_path.name, f.read()),
            model=settings.whisper_model,
            prompt=prompt or "",
            # response_format="text" вернул бы только строку,
            # но verbose_json полезен если позже понадобятся сегменты
            # с таймкодами (например, для командной диктовки).
            response_format="text",
            temperature=0.0,
        )
    text = str(response).strip()
    log.info("Whisper: got %d chars", len(text))
    return text
