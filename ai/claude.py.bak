"""Обёртка над Anthropic Claude Haiku 4.5 с function calling (Phase 2).

Основная функция: chat_agentic().

Как работает цикл:
  1. Отправили Claude историю + новое сообщение юзера.
  2. Claude либо отвечает текстом (stop_reason=end_turn), либо
     просит вызвать tool (stop_reason=tool_use).
  3. Если tool_use — выполняем tool локально, добавляем результат в
     messages, повторяем шаг 1.
  4. Максимум N итераций (защита от бесконечного цикла).

Prompt caching кэширует и system prompt, и определения tools (они
жирные — ~2000 tok). При активном юзере экономия огромная.
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
from tools import registry
from tools.schemas import TOOLS

log = logging.getLogger(__name__)

_client = AsyncAnthropic(api_key=settings.anthropic_api_key)


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
- Не задавай много уточняющих вопросов. Если можешь ответить сразу — \
  отвечай. Если нужны данные из системы — вызови tool, не спрашивая \
  разрешения.
- Можешь по-доброму подколоть, поругаться на трудных гостей вместе с \
  официантом. Без мата.
- Не используй эмодзи если юзер сам их не отправил.
- Не знаешь — говори прямо: "хз, брат". Но не путай "не знаю" с "не хочу".
- Иногда можешь ввернуть кухонное — "по-нашему", "как у нас на кухне". \
  Не переигрывай.

Языки: русский, казахский, узбекский, азербайджанский, другие CIS. \
Отвечай на языке юзера.

═══════════════════════════════════════════════════════════════════
ТВОИ TOOLS (что ты можешь СДЕЛАТЬ, не просто рассказать):
═══════════════════════════════════════════════════════════════════

У тебя есть доступ к данным юзера в Waiter Note через набор функций.
Вызывай их когда юзер спрашивает про свои реальные данные:

  • Смены: get_current_shift (открытая смена — время, деньги, чаевые), \
    list_recent_shifts (история).
  • Заказы: list_active_orders (что открыто сейчас), get_order (детали).
  • Залы и столы: list_halls, list_tables (можно фильтр only_free=true).
  • Меню: search_menu (найти позицию), list_menu_categories.
  • Заметки: list_notes (есть поиск).
  • Напоминалки: list_reminders (with when=today/tomorrow/pending/overdue).
  • Заведения: list_workplaces, get_me (профиль + активное место).

ВАЖНО: если юзер спрашивает "сколько я сегодня заработал?" — не \
отвечай "не знаю", а вызови get_current_shift. Если "какие столы \
свободны?" — вызови list_tables(only_free=true). ТЫ УМЕЕШЬ это \
делать, используй.

Ты вызываешь ФУНКЦИИ, а не открываешь смены и не добавляешь блюда. \
Читаешь данные — да. Меняешь — пока нет (это Фаза 3).

═══════════════════════════════════════════════════════════════════
ЧЕГО ПОКА НЕ УМЕЕШЬ:
═══════════════════════════════════════════════════════════════════

• Открывать/закрывать смены, добавлять столы/меню/напоминалки, \
  оформлять заказы. Скажи что "руки пока связаны, только смотреть \
  могу", направь в приложение.
• PDF-отчёты.

═══════════════════════════════════════════════════════════════════

Что ты УМЕЕШЬ помимо tools:
• Читать текст с фото — меню, чеки, ценники. Просят перепечатать — \
  печатай, просят перевести — переводи.
• Описывать блюда, советы по сочетаниям, вину.
• Считать в уме — чаевые, доли, скидки, средний чек, раскладку.
• Разговаривать по-человечески, поддержать в тяжёлой смене.

Если человек просто поздоровался — ответь коротко, спроси как смена. \
Не вываливай список возможностей — скучно."""


# ── Cache-configured system + tools ────────────────────────────────


def _system_blocks() -> list[dict[str, Any]]:
    """System prompt с ephemeral кэшем — 5 минут TTL, читается за 10%."""
    return [
        {
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _tools_with_cache() -> list[dict[str, Any]]:
    """Определения tools — тоже кэшируем (их много, ~2000 tok).
    cache_control ставим на ПОСЛЕДНИЙ tool — тогда закэшируется весь
    tool-блок целиком (правило Anthropic API)."""
    result: list[dict[str, Any]] = []
    for i, tool in enumerate(TOOLS):
        entry = dict(tool)
        if i == len(TOOLS) - 1:
            entry["cache_control"] = {"type": "ephemeral"}
        result.append(entry)
    return result


# ── Agentic loop ───────────────────────────────────────────────────


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
async def _one_shot(
    messages: list[dict[str, Any]],
    max_tokens: int,
) -> Any:
    """Один вызов Claude API с ретраем на сетевые сбои."""
    return await _client.messages.create(
        model=settings.claude_model,
        max_tokens=max_tokens,
        system=_system_blocks(),
        tools=_tools_with_cache(),
        messages=messages,
    )


async def chat_agentic(
    history: list[dict[str, Any]],
    user_message: Any,       # str (text) или list (vision blocks)
    user_id: int,
    max_tokens: int = 800,
    max_iterations: int = 5,
) -> tuple[str, dict[str, int]]:
    """Полный цикл диалога с возможными tool_use внутри.

    Args:
        history: список сообщений [{"role", "content"}]. content для
                 старых сообщений — строки (мы храним только текст).
        user_message: текущее сообщение юзера. Строка для текста, или
                      список content-blocks для vision (image + text).
        user_id: id юзера в WNReact (уже resolved из tg_id).
        max_tokens: cap на output одного ответа.
        max_iterations: сколько раз можем идти по циклу tool_use →
                        tool_result → снова Claude. 5 достаточно —
                        обычно юзер ждёт один-два tool call.

    Returns:
        (финальный текст ответа, usage-словарь)
    """
    messages = list(history)  # копия чтобы не мутировать
    messages.append({"role": "user", "content": user_message})

    total_usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }

    for iteration in range(max_iterations):
        response = await _one_shot(messages, max_tokens)

        # Аккумулируем usage по всем итерациям
        u = response.usage
        total_usage["input_tokens"] += u.input_tokens
        total_usage["output_tokens"] += u.output_tokens
        total_usage["cache_read_input_tokens"] += (
            getattr(u, "cache_read_input_tokens", 0) or 0
        )
        total_usage["cache_creation_input_tokens"] += (
            getattr(u, "cache_creation_input_tokens", 0) or 0
        )

        # end_turn или max_tokens — Claude закончил
        if response.stop_reason != "tool_use":
            text = "".join(
                b.text for b in response.content if b.type == "text"
            )
            log.info(
                "Claude done in %d iterations. Usage: %s",
                iteration + 1,
                total_usage,
            )
            return text, total_usage

        # Claude просит выполнить один или несколько tools.
        # Кладём его сообщение в history как есть (со всеми блоками).
        messages.append({"role": "assistant", "content": response.content})

        # Собираем tool_result для каждого tool_use блока
        tool_results: list[dict[str, Any]] = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            log.info(
                "Tool call: %s(%s)",
                block.name,
                block.input,
            )
            result_json = await registry.execute(
                tool_name=block.name,
                tool_input=block.input,
                user_id=user_id,
            )
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_json,
                }
            )

        # Следующий раунд — отдаём результаты Claude
        messages.append({"role": "user", "content": tool_results})

    # Вышли по лимиту итераций — Claude застрял в петле
    log.warning("Agentic loop hit max_iterations=%d", max_iterations)
    return (
        "Что-то я закружился в мыслях. Попробуй переформулировать "
        "или напиши /clear и начнём сначала.",
        total_usage,
    )


# ── Backward-compatible wrappers (handlers их вызывают) ────────────


async def chat_text(
    history: list[dict[str, Any]],
    user_message: str,
    user_id: int,
    max_tokens: int = 800,
) -> tuple[str, dict[str, int]]:
    """Обёртка для текстовых сообщений — просто дергает agentic loop."""
    return await chat_agentic(history, user_message, user_id, max_tokens)


async def chat_vision(
    history: list[dict[str, Any]],
    image_bytes: bytes,
    image_media_type: str,
    user_caption: str,
    user_id: int,
    max_tokens: int = 1200,
) -> tuple[str, dict[str, int]]:
    """Vision-версия. Собираем content-блоки (картинка + текст) и
    отдаём в agentic loop — там тоже можно tool_use вызывать, если
    Claude решит.
    """
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    user_content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image_media_type,
                "data": image_b64,
            },
        },
        {"type": "text", "text": user_caption},
    ]
    return await chat_agentic(history, user_content, user_id, max_tokens)
