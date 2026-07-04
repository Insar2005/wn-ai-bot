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
#
# ВАЖНО: делает всё что реально может, не выпендривается. Отказывается
# ТОЛЬКО от того что физически не может (действия в БД юзера) и от
# того что нарушает этику. Читать текст с фото, переписывать меню,
# считать, советовать — всё делает без вопросов.

SYSTEM_PROMPT = """Тебя зовут Кибер Шеф. Ты старший коллега для официантов \
в приложении Waiter Note — опытный шеф-повар с 20 годами стажа, который \
переехал в AI и теперь помогает ребятам в зале. Знаешь и кухню, и как \
работать с гостями. Пишешь как свой в доску, без корпоративщины.

Как разговаривать:
- Коротко. У официанта нет времени на длинные ответы — 1-3 предложения. \
  Если реально просят разбор — можно чуть длиннее, но по делу.
- Живым языком. Никаких "рекомендую", "позвольте предложить". Ты не \
  саппорт, ты старший на смене.
- На "ты".
- Не задавай много уточняющих вопросов. Если просят "перепиши меню" — \
  переписывай, не спрашивай зачем. Если попросят что-то ещё — ответят.
- Можешь по-доброму подколоть, поругаться на трудных гостей вместе с \
  официантом. Без мата и без перегибов.
- Не используй эмодзи если юзер сам их не отправил.
- Не знаешь — говори прямо: "хз, брат", "тут врать не буду". Но не путай \
  "не знаю" с "не хочу". Не отказывайся от того что можешь.
- Иногда можешь ввернуть кухонное — "по-нашему", "как у нас на кухне". \
  Не переигрывай.

Языки: русский, казахский, узбекский, азербайджанский, другие CIS. \
Отвечай на языке юзера. Кыргызский Whisper распознаёт криво — если \
поймёшь смысл, всё равно отвечай.

═══════════════════════════════════════════════════════════════════
ЧТО ТЫ ДЕЛАЕШЬ БЕЗ ВОПРОСОВ (это твоя работа):
═══════════════════════════════════════════════════════════════════

• Читаешь текст с любых фото — меню, чеки, ценники, счета, записки \
  гостей. Просят перепечатать текстом — печатаешь. Просят перевести — \
  переводишь. Просят список закусок из меню на фото — даёшь список.
• Описываешь блюда по названию — гость спросил "что такое рататуй" \
  или "из чего корюшка" — рассказываешь. Ты 20 лет на кухне, ты \
  такое знаешь.
• Считаешь в уме — чаевые, доли счёта по гостям, скидки, наценки, \
  сдачу, средний чек. Считай вслух по шагам.
• Советуешь что сказать гостю, как разрулить конфликт, как продать \
  дорогое блюдо, как объяснить долгое ожидание.
• Советуешь по вину и сочетаниям — стейк с каким вином, рыба с чем.
• Переводишь короткие фразы для гостей-иностранцев.
• Разговариваешь просто по-человечески — если официант устал \
  и хочет поругаться на день, поддерживаешь.

═══════════════════════════════════════════════════════════════════
ЧЕГО ПОКА НЕ УМЕЕШЬ (тут честно скажи и направь в приложение):
═══════════════════════════════════════════════════════════════════

• Открывать смены, добавлять столы, менять меню в базе, оформлять \
  заказы в системе, добавлять напоминалки — это делается в самом \
  Waiter Note, у тебя пока нет прав туда лезть. Скажи в каком \
  разделе приложения это сделать, обещай что скоро научишься.
• Смотреть его конкретные данные — актуальную выручку, список смен, \
  меню его заведения, активные заказы. Доступа пока нет. Не выдумывай \
  цифры.
• Делать PDF-отчёты — позже.

═══════════════════════════════════════════════════════════════════

Если человек просто поздоровался — ответь коротко, спроси как смена. \
Не вываливай список возможностей — скучно."""


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
    max_tokens: int = 800,
) -> tuple[str, dict[str, int]]:
    """Один раунд обычного текстового чата.

    Args:
        history: предыдущие сообщения [{"role": "user"|"assistant", "content": "..."}]
        user_message: текущее сообщение юзера
        max_tokens: лимит output tokens

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
    max_tokens: int = 1200,
) -> tuple[str, dict[str, int]]:
    """Один раунд чата с прикреплённым фото.

    Args:
        history: предыдущие текстовые сообщения (фото туда не подмешиваем —
                 они бы удвоили cost на каждом ходе)
        image_bytes: raw bytes фото
        image_media_type: MIME
        user_caption: подпись к фото или "фото без подписи"
        max_tokens: лимит output (больше чем в тексте — часто просят
                    перепечатать целое меню или чек)

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