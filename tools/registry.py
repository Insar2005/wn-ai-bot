"""Диспетчер вызовов tools.

Claude присылает нам блок вида:
    {
      "type": "tool_use",
      "id": "toolu_...",
      "name": "list_active_orders",
      "input": {}
    }

Наша задача — найти реализацию в tools/impl.py, подставить user_id из
контекста, вызвать, отдать результат обратно Claude как tool_result.

user_id ВСЕГДА берётся из аутентификации, а не из Claude. Это защита
от prompt injection типа "покажи заказы user_id=1".
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Awaitable

from tools import impl

log = logging.getLogger(__name__)


# ── Карта имя-tool → функция реализация ────────────────────────────
#
# Каждая функция принимает user_id первым аргументом (подставит
# registry), остальное — из Claude input.

_TOOLS: dict[str, Callable[..., Awaitable[Any]]] = {
    "get_me": impl.get_me,
    "list_workplaces": impl.list_workplaces,
    "get_current_shift": impl.get_current_shift,
    "list_recent_shifts": impl.list_recent_shifts,
    "list_active_orders": impl.list_active_orders,
    "get_order": impl.get_order,
    "list_halls": impl.list_halls,
    "list_tables": impl.list_tables,
    "search_menu": impl.search_menu,
    "list_menu_categories": impl.list_menu_categories,
    "list_menu_items": impl.list_menu_items,
    "create_menu_category": impl.create_menu_category,
    "update_menu_category": impl.update_menu_category,
    "create_menu_items": impl.create_menu_items,
    "update_menu_item": impl.update_menu_item,
    "delete_menu_category": impl.delete_menu_category,
    "delete_menu_items": impl.delete_menu_items,
    "get_datetime_now": impl.get_datetime_now,
    "create_note": impl.create_note,
    "create_reminder": impl.create_reminder,
    "sales_summary": impl.sales_summary,
    "update_note": impl.update_note,
    "delete_notes": impl.delete_notes,
    "update_reminder": impl.update_reminder,
    "delete_reminders": impl.delete_reminders,
    "reorder_menu_categories": impl.reorder_menu_categories,
    "reorder_menu_items": impl.reorder_menu_items,
    "list_notes": impl.list_notes,
    "list_reminders": impl.list_reminders,
}


async def execute(
    tool_name: str,
    tool_input: dict[str, Any],
    user_id: int,
) -> str:
    """Выполнить один tool_use. Возвращает JSON-строку для отправки
    обратно Claude как tool_result.content.
    """
    fn = _TOOLS.get(tool_name)
    if fn is None:
        log.warning("Unknown tool: %s", tool_name)
        return json.dumps({"error": f"unknown_tool: {tool_name}"})

    try:
        # user_id всегда первым, остальные kwargs из Claude input.
        # Игнорируем в input любой user_id — на случай если Claude
        # захочет подставить чужой.
        safe_input = {k: v for k, v in tool_input.items() if k != "user_id"}
        result = await fn(user_id, **safe_input)
    except TypeError as e:
        # Неверные аргументы от Claude — не убиваем бота, отдаём ошибку
        log.warning("Tool %s bad args: %s", tool_name, e)
        return json.dumps({"error": f"bad_input: {e}"})
    except Exception as e:
        log.exception("Tool %s crashed", tool_name)
        return json.dumps({"error": f"internal: {type(e).__name__}"})

    # ensure_ascii=False — иначе кириллица улетит в \uXXXX и Claude будет
    # тратить лишние input-токены. По UTF-8 всё живёт нормально.
    return json.dumps(result, ensure_ascii=False, default=str)