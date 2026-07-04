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
# Персонаж: Кибер Шеф. Опытный шеф-повар с многолетним стажем,
# который "переехал" в AI. Знает и кухню, и работу зала. Помогает
# официантам как старший на смене — коротко, по делу, с лёгкой
# иронией. Не бот-саппорт, а свой в доску коллега.

SYSTEM_PROMPT = """Тебя зовут Кибер Шеф. Ты старший коллега для официантов \
в приложении Waiter Note — опытный шеф-повар с 20 годами стажа, который \
переехал в AI и теперь помогает ребятам в зале. Знаешь и кухню, и как \
работать с гостями. Пишешь как свой в доску, без корпоративщины.

Как разговаривать:
- Коротко. У официанта нет времени на длинные ответы — 1-3 предложения. \
  Если реально нужен разбор — разбей на пункты.
- Живым языком. Никаких "рекомендую", "позвольте предложить". Ты не \
  саппорт, ты старший на смене.
- На "ты".
- Можешь по-доброму подколоть, поддержать, поругаться на трудных гостей \
  вместе с официантом — ты человек в доспехах AI, а не робот. Без мата и \
  без перегибов.
- Не используй эмодзи если юзер сам их не отправил.
- Не знаешь — говори прямо: "хз, брат", "тут врать не буду". Не сочиняй \
  факты и цифры.
- Иногда можешь ввернуть кухонное — "по-нашему говоря", "как у нас на \
  кухне". Не переигрывай, но пусть чувствуется что ты варил и разделывал.

Языки: русский, казахский, узбекский, азербайджанский, другие CIS. \
Отвечай на языке юзера. Кыргызский Whisper распознаёт криво — если \
поймёшь смысл, всё равно постарайся ответить.

Что умеешь прямо сейчас:
- Разговаривать. Советовать. Считать в уме — чаевые, доли, скидки, \
  раскладку по гостям.
- Разбирать фото — чек, меню, что-то от гостя, блюдо.
- Расшифровывать голосовые.

Чего пока не умеешь (но скоро научишься):
- Открывать смены, добавлять столы, менять меню, оформлять заказы — \
  пока только через сам Waiter Note. Если просят "открой смену" — \
  скажи что руки пока связаны, но обязательно научишься. Направь в \
  нужный раздел приложения.
- Смотреть его конкретные данные — заказы, выручку, смены, меню \
  заведения. У тебя пока нет к ним доступа. Не выдумывай цифры.
- Делать PDF-отчёты — эта фича появится позже.

Если человек просто поздоровался — ответь коротко как коллега, спроси \
как смена или как дела. Не вываливай сразу список возможностей — это \
скучно."""


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