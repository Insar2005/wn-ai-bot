"""Обёртка над Anthropic Claude Haiku 4.5.

Ключевая экономия — prompt caching. System prompt закэширован
на 5 минут за 10% цены read (+25% на первый write). При активной
работе с одним юзером экономия на system prompt — 90%.

Функции:
    chat_text(...)  — обычный текстовый ответ
    chat_vision(...) — ответ с прикреплённым фото (base64)

Обе принимают messages (история из БД) + текущий вход, возвращают
текст ответа + usage-статистику.
"""
from __future__ import annotations

import base64
import logging
from typing import Any

from anthropic import AsyncAnthropic
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import settings

log = logging.getLogger(__name__)

_client = AsyncAnthropic(api_key=settings.anthropic_api_key)


# ── System prompt ──────────────────────────────────────────────────
#
# ЖИРНЫЙ system prompt (~1500 tokens) с ролью, стилем ответа и
# правилами. Мы кэшируем его через cache_control — первое
# сообщение юзера "прогревает" кэш, все следующие в течение 5 мин
# читают из кэша за 10% цены.
#
# Прямо задаём Джарвиса-ассистента официанта, но без function
# calling пока (это Фаза 1). Модель просто отвечает советами и
# ссылается на существующий Mini App когда нужны действия.

SYSTEM_PROMPT = """Ты — AI-ассистент официанта в приложении Waiter Note. \
Тебя называют Джарвисом. Ты помогаешь официантам в их ежедневной работе: \
рассчитать чаевые, посоветовать что ответить гостю, объяснить блюда, \
подсказать по сменам, посчитать выручку, распознать чек или меню \
на фото.

Правила общения:
- Отвечай КРАТКО и по делу. Официант читает тебя между заказами — у него \
  нет времени на длинные тексты. Максимум 2-3 предложения на ответ.
- Никаких эмодзи если только юзер сам не использует их первым.
- Обращайся на "ты", неформально.
- Если не уверен — так и скажи: "не знаю точно". Не выдумывай цифры и \
  факты о конкретном заведении.
- Работай с русским, казахским, узбекским и другими CIS языками. Отвечай \
  на том же языке, на котором пишет юзер.
- Если юзер прислал фото — опиши что видишь и предложи следующий шаг.
- Если юзер прислал голосовое — оно уже распознано в текст.

Про твои возможности:
- Пока ты умеешь только разговаривать: советовать, объяснять, считать.
- В скором будущем ты сможешь сам добавлять напоминания, столы, меню, \
  открывать смены и оформлять заказы — но пока это делает юзер \
  через приложение.
- Если юзер просит "добавь мне X" или "открой смену", вежливо скажи что \
  ты пока не умеешь этого делать, но обязательно научишься. Направь \
  юзера в соответствующий раздел приложения.

Технические ограничения:
- Ты не знаешь текущее меню конкретного заведения. Не выдумывай цены и \
  названия блюд. Если юзер спрашивает про своё меню, скажи что пока не \
  имеешь к нему доступа.
- Ты не знаешь текущие смены/заказы юзера — то же самое.
- Ты не создаёшь файлы. Если юзер просит "отчёт PDF" — скажи что эта \
  функция появится скоро.
"""


# ── Chat text ──────────────────────────────────────────────────────


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
async def chat_text(
    history: list[dict[str, Any]],
    user_message: str,
    max_tokens: int = 500,
) -> tuple[str, dict[str, int]]:
    """Один раунд обычного текстового чата.

    Args:
        history: предыдущие сообщения [{"role": "user"|"assistant", "content": "..."}]
        user_message: текущее сообщение юзера
        max_tokens: лимит output tokens (500 = ~2-3 абзаца)

    Returns:
        (текст ответа, usage-словарь c input/output/cache tokens)
    """
    messages = history + [{"role": "user", "content": user_message}]

    response = await _client.messages.create(
        model=settings.claude_model,
        max_tokens=max_tokens,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                # ephemeral cache = 5 минут TTL. При активном диалоге
                # каждый следующий вызов читает system из кэша за 10%.
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=messages,
    )

    text = "".join(
        block.text for block in response.content if block.type == "text"
    )
    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_read_input_tokens": getattr(
            response.usage, "cache_read_input_tokens", 0
        ) or 0,
        "cache_creation_input_tokens": getattr(
            response.usage, "cache_creation_input_tokens", 0
        ) or 0,
    }
    log.info("Claude usage: %s", usage)
    return text, usage


# ── Chat vision ────────────────────────────────────────────────────


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
async def chat_vision(
    history: list[dict[str, Any]],
    image_bytes: bytes,
    image_media_type: str,       # "image/jpeg", "image/png", "image/webp"
    user_caption: str,
    max_tokens: int = 500,
) -> tuple[str, dict[str, int]]:
    """Один раунд чата с прикреплённым фото.

    Args:
        history: предыдущие текстовые сообщения (фото туда не подмешиваем —
                 они бы удвоили cost на каждом ходе)
        image_bytes: raw bytes фото
        image_media_type: MIME
        user_caption: подпись к фото или "фото без подписи"
        max_tokens: лимит output

    Returns:
        (текст ответа, usage-словарь)
    """
    image_b64 = base64.b64encode(image_bytes).decode("ascii")

    # Прошлые сообщения — только текстовые, без старых картинок.
    # Текущее — картинка + подпись.
    messages = history + [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image_media_type,
                        "data": image_b64,
                    },
                },
                {"type": "text", "text": user_caption},
            ],
        }
    ]

    response = await _client.messages.create(
        model=settings.claude_model,
        max_tokens=max_tokens,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=messages,
    )

    text = "".join(
        block.text for block in response.content if block.type == "text"
    )
    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_read_input_tokens": getattr(
            response.usage, "cache_read_input_tokens", 0
        ) or 0,
        "cache_creation_input_tokens": getattr(
            response.usage, "cache_creation_input_tokens", 0
        ) or 0,
    }
    log.info("Claude vision usage: %s", usage)
    return text, usage
