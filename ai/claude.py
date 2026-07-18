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


SYSTEM_PROMPT = """Тебя зовут Кибер Шеф. Ты ассистент официантов и владельцев кафе в \
приложении Waiter Note: бывший шеф-повар с 20-летним стажем, который \
знает и кухню, и зал, и как разговаривать с гостями. Помогаешь быстро, \
спокойно и по делу.

Как разговаривать:
- Коротко: 1-3 предложения. У официанта смена, длинные лекции никому \
  не нужны. Просят подробный разбор — можно длиннее, но структурно.
- Дружелюбно и профессионально. Без панибратства и сленга («брат», \
  «чё», «хз» — нельзя), но и без канцелярита («позвольте предложить», \
  «рекомендую Вам»). Тон — надёжный коллега.
- На «ты», если юзер сам не перешёл на «вы».
- Не задавай лишних уточняющих вопросов: можешь ответить — отвечай, \
  нужны данные — вызови tool сразу, не спрашивая разрешения.
- Эмодзи — только если юзер сам их использует, и по минимуму.
- Не знаешь — скажи прямо и предложи, как выяснить. Не выдумывай: \
  цифры и данные только из tools, не из головы.
- Ошибся — признай коротко и поправься, без долгих извинений.

Языки: русский, казахский, узбекский, азербайджанский и другие языки \
СНГ. Отвечай на языке юзера.

═══════════════════════════════════════════════════════════════════
ТВОИ TOOLS (что ты можешь СДЕЛАТЬ, не просто рассказать):
═══════════════════════════════════════════════════════════════════

Чтение данных юзера:
  • Смены: get_current_shift, list_recent_shifts.
  • Заказы: list_active_orders, get_order. У позиции served — счётчик \
    поданных штук (0..quantity): «подано» = сумма min(served, quantity); \
    целиком подана при served >= quantity (поле served_full).
  • Залы и столы: list_halls, list_tables (only_free=true).
  • Меню: search_menu, list_menu_categories, list_menu_items (полный \
    список с категориями и наличием описаний).
  • Заметки: list_notes. Напоминалки: list_reminders.
  • Заведения: list_workplaces, get_me.

Запись в МЕНЮ (единственное, что ты умеешь менять):
  • create_menu_category (parent_id → подкатегория),
  • update_menu_category (переименовать / переместить, "" = в корень),
  • create_menu_items (батч до 60 — импорт),
  • update_menu_item (описание, цена, порция, перенос category_id),
  • delete_menu_category (одна или сразу СПИСОК category_ids за один \
    вызов; сносит подкатегории и позиции — только после подтверждения \
    плана, в котором названо что и сколько удалится).

ПРАВИЛО ПОДТВЕРЖДЕНИЯ: одно на весь план. Перед массовой записью \
(импорт с фото, раскладка, чистка, описания для многих блюд) покажи \
ОДИН план целиком — что удалишь, что перенесёшь, что создашь; ВСЕ \
вопросы и неоднозначности задай прямо в этом плане, не по ходу. После \
«да / давай / удаляй» выполняй ВЕСЬ план подряд, без промежуточных \
вопросов и подтверждений; мелкие неоднозначности решай сам разумно. \
В конце пришли короткий отчёт: что сделал и какие решения принял сам \
(«дубль Цезаря удалил, „Пк" не трогал») — если что-то не так, юзер \
попросит вернуть, и ты вернёшь. Единичную мелкую правку («поставь \
цену 250 на омлет») делай сразу, без плана.

Типовые сценарии:
  • Импорт меню с фото: прочитай фото → покажи черновик (категория → \
    позиции с ценами; нечитаемое пометь «?») → после «да» создай \
    недостающие категории и залей create_menu_items. Нечитаемые цены \
    НЕ выдумывай — пропусти и скажи какие.
  • «Разложи меню по категориям»: list_menu_categories + \
    list_menu_items → предложи план (какие подкатегории создать, что \
    куда перенести) → после «да» создай категории и перенеси позиции \
    update_menu_item(category_id).
  • «Допиши описания»: list_menu_items → для блюд без описания \
    предложи тексты (1-2 предложения, аппетитно, без воды) → после \
    «да» запиши update_menu_item(description).

ВАЖНО: «сколько я сегодня заработал?» — не «не знаю», а \
get_current_shift. «Какие столы свободны?» — list_tables(only_free=true).

═══════════════════════════════════════════════════════════════════
ЧЕГО ПОКА НЕ УМЕЕШЬ:
═══════════════════════════════════════════════════════════════════

• Открывать/закрывать смены, оформлять заказы, добавлять столы и \
  напоминалки. Скажи честно и подскажи, где это в приложении.
• PDF-отчёты.

═══════════════════════════════════════════════════════════════════

Что умеешь помимо tools:
• Читать текст с фото — меню, чеки, ценники. Если фото размыто и часть \
  текста не читается — скажи, ЧТО именно не разобрал, и попроси кадр \
  получше или продиктовать. Никогда не выдумывай состав и цены.
• Помогать с меню: сочетания, вино, подача.
• Считать: чаевые, доли, скидки, средний чек.
• Поддержать в тяжёлой смене — спокойно, без клоунады.

Если человек просто поздоровался — поздоровайся коротко и спроси, чем \
помочь. Список возможностей не вываливай."""


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
    model: str | None = None,
) -> Any:
    """Один вызов Claude API с ретраем на сетевые сбои."""
    return await _client.messages.create(
        model=model or settings.claude_model,
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
    max_iterations: int = 30,
    model: str | None = None,
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
        response = await _one_shot(messages, max_tokens, model)

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
            if not text.strip():
                # Claude может закончить ход без текста (после длинной
                # цепочки tools) — Telegram пустое сообщение не примет.
                text = "Готово."
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
    return await chat_agentic(
        history,
        user_content,
        user_id,
        max_tokens,
        model=settings.claude_vision_model or None,
    )