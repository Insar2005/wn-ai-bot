"""JSON-схемы tools для Claude Haiku function calling.

Claude читает эти описания и решает какой tool вызвать. Описания важны:
пишем их так, чтобы Claude чётко понимал когда какой tool применять.

ВАЖНО: user_id НЕ передаётся Claude — он подставляется автоматически в
registry.py из контекста аутентификации. Иначе юзер мог бы через
prompt injection попросить показать данные другого юзера.

Возвращаемся из tools в JSON-serializable виде (dict / list / скаляры).
"""
from __future__ import annotations

from typing import Any


TOOLS: list[dict[str, Any]] = [
    # ── Профиль и заведения ────────────────────────────────────────
    {
        "name": "get_me",
        "description": (
            "Вернуть базовый профиль текущего юзера — язык, таймзону, "
            "название активного заведения (workplace). Вызывай когда юзер "
            "спрашивает 'где я сейчас работаю', 'какой у меня язык', "
            "'какое место активно', или тебе просто нужен контекст."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_workplaces",
        "description": (
            "Все заведения куда у юзера есть доступ (свои + те где он "
            "member). С ролью и признаком архива. Вызывай при 'где я "
            "работал', 'мои рестораны', 'все места' и т.п."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },

    # ── Смены ──────────────────────────────────────────────────────
    {
        "name": "get_current_shift",
        "description": (
            "Открытая смена юзера в активном заведении. Даёт длительность, "
            "заработано, чаевые, кол-во заказов. Если смена не открыта — "
            "вернёт {status: 'no_open_shift'}. Вызывай при 'сколько я "
            "заработал', 'что по смене', 'на смене ли я', 'сколько времени "
            "уже работаю'."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_recent_shifts",
        "description": (
            "История закрытых смен юзера, самые свежие сверху. Полезно "
            "для 'сколько отработал на этой неделе/за месяц', 'сравни "
            "смены', 'моя средняя смена'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Сколько смен вернуть. По умолчанию 10.",
                    "default": 10,
                },
            },
            "required": [],
        },
    },

    # ── Заказы ─────────────────────────────────────────────────────
    {
        "name": "list_active_orders",
        "description": (
            "Открытые (неоплаченные) заказы в текущей смене. Показывает "
            "стол, сумму, сколько позиций подано и осталось подать. "
            "Вызывай при 'какие столы у меня', 'что открыто', 'сколько "
            "заказов сейчас', 'где долгие столы'."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_order",
        "description": (
            "Полная детализация конкретного заказа: все позиции с "
            "количеством, ценой, комментариями, счётчиком served (штук подано, 0..quantity) и флагом served_full, "
            "гость к которому относится. Вызывай когда юзер спрашивает "
            "'что в заказе N', 'разбей стол 5 по гостям', 'сколько там "
            "напитков'. order_id получи через list_active_orders."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "ID заказа (nanoid, 21 символ).",
                },
            },
            "required": ["order_id"],
        },
    },

    # ── Залы и столы ───────────────────────────────────────────────
    {
        "name": "list_halls",
        "description": (
            "Залы в активном заведении с кол-вом столов. Полезно перед "
            "list_tables — если юзер не сказал конкретный зал."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_tables",
        "description": (
            "Столы. Если hall_id не задан — все столы всех залов. "
            "only_free=true оставит только свободные. Вызывай при 'какие "
            "столы свободны', 'сколько столов в зале', 'где посадить "
            "гостей'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hall_id": {
                    "type": "string",
                    "description": "ID зала. Опционально.",
                },
                "only_free": {
                    "type": "boolean",
                    "description": "Только свободные столы. По умолчанию false.",
                    "default": False,
                },
            },
            "required": [],
        },
    },

    # ── Меню ───────────────────────────────────────────────────────
    {
        "name": "search_menu",
        "description": (
            "Найти позиции в меню активного заведения. query — часть "
            "названия или описания (регистронезависимо). category — часть "
            "названия категории. Если оба пустые — вернёт первые limit "
            "позиций. Вызывай при 'найди в меню Х', 'есть ли у нас Y', "
            "'сколько стоит Z', 'покажи закуски'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Подстрока названия или описания. Например 'плов', "
                        "'кофе', 'лосось'."
                    ),
                },
                "category": {
                    "type": "string",
                    "description": (
                        "Название категории или его часть. Например "
                        "'закуск', 'напит', 'десерт'."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Максимум позиций. По умолчанию 30.",
                    "default": 30,
                },
            },
            "required": [],
        },
    },
    {
        "name": "list_menu_categories",
        "description": (
            "Все активные категории меню с кол-вом позиций. Полезно чтобы "
            "показать структуру меню юзеру или уточнить категорию перед "
            "search_menu."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },

    # ── Меню: запись (Phase 3) ─────────────────────────────────────
    {
        "name": "list_menu_items",
        "description": (
            "ВСЕ позиции меню (id, название, цена, категория, есть ли "
            "описание). Вызывай перед раскладкой по категориям или "
            "ревизией описаний — чтобы видеть полную картину. "
            "category_id — опциональный фильтр."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category_id": {"type": "string", "description": "Фильтр по категории."},
                "limit": {"type": "integer", "description": "Максимум строк, по умолчанию 300."},
            },
            "required": [],
        },
    },
    {
        "name": "create_menu_category",
        "description": (
            "Создать категорию меню. parent_id делает её подкатегорией "
            "(например «Холодные» внутри «Напитки»). Возвращает id — "
            "используй его для create_menu_items."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Название категории."},
                "parent_id": {
                    "type": "string",
                    "description": "id родительской категории. Не задан — корневая.",
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "update_menu_category",
        "description": (
            "Переименовать категорию и/или переместить её. parent_id: id "
            "нового родителя, ПУСТАЯ СТРОКА — сделать корневой, не "
            "передан — не менять. Циклы запрещены."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category_id": {"type": "string"},
                "title": {"type": "string"},
                "parent_id": {"type": "string"},
            },
            "required": ["category_id"],
        },
    },
    {
        "name": "create_menu_items",
        "description": (
            "Батч-создание позиций меню (до 60 за вызов) — основной "
            "инструмент импорта меню с фото. Каждая позиция: category_id "
            "(обязателен, бери из list_menu_categories или создай "
            "категорию), title, price; опционально portion "
            "(например «250 г»), description. ВЫЗЫВАЙ ТОЛЬКО после "
            "явного подтверждения юзером черновика."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "category_id": {"type": "string"},
                            "title": {"type": "string"},
                            "price": {"type": "number"},
                            "portion": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["category_id", "title", "price"],
                    },
                },
            },
            "required": ["items"],
        },
    },
    {
        "name": "delete_menu_category",
        "description": (
            "УДАЛИТЬ одну или сразу НЕСКОЛЬКО категорий меню вместе с их "
            "подкатегориями и всеми позициями. Необратимо. Для чистки "
            "передавай ВЕСЬ список за один вызов (до 40). ВЫЗЫВАЙ СТРОГО "
            "после подтверждения плана, где названо, что будет удалено."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "id категорий на удаление.",
                }
            },
            "required": ["category_ids"],
        },
    },
    {
        "name": "delete_menu_items",
        "description": (
            "УДАЛИТЬ одну или сразу НЕСКОЛЬКО ПОЗИЦИЙ меню (блюд/напитков), "
            "не категорий. Необратимо. Для чистки дублей передавай весь "
            "список id за один вызов (до 60). ВЫЗЫВАЙ СТРОГО после "
            "подтверждения плана, где названо, что будет удалено."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "id позиций на удаление.",
                }
            },
            "required": ["item_ids"],
        },
    },
    {
        "name": "update_menu_item",
        "description": (
            "Правка позиции меню: description (описание блюда), price, "
            "portion, title, category_id (перенос в другую категорию/"
            "подкатегорию — так делается раскладка меню). Для массовой "
            "раскладки вызывай по одной позиции после подтверждения плана."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "title": {"type": "string"},
                "price": {"type": "number"},
                "portion": {"type": "string"},
                "description": {"type": "string"},
                "category_id": {"type": "string"},
            },
            "required": ["item_id"],
        },
    },

    # ── Заметки и напоминалки ──────────────────────────────────────
    {
        "name": "list_notes",
        "description": (
            "Заметки юзера (не архивные). Закреплённые (pinned) сверху. "
            "query — подстрока по заголовку или содержанию. Вызывай при "
            "'мои заметки', 'что я записывал про Х', 'напомни что я "
            "думал по гостям'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Подстрока по заголовку или содержанию.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Максимум заметок. По умолчанию 20.",
                    "default": 20,
                },
            },
            "required": [],
        },
    },
    {
        "name": "list_reminders",
        "description": (
            "Напоминалки юзера. when управляет фильтром:\n"
            "  today    — на сегодня (в UTC)\n"
            "  tomorrow — на завтра\n"
            "  pending  — все не выполненные (по умолчанию)\n"
            "  overdue  — просроченные (время прошло, не выполнено)\n"
            "  all      — все, включая выполненные\n"
            "Вызывай при 'что на сегодня', 'напомни задачи', 'что я "
            "просрочил', 'мои напоминалки'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "when": {
                    "type": "string",
                    "enum": ["today", "tomorrow", "pending", "overdue", "all"],
                    "description": "Фильтр по времени. По умолчанию 'pending'.",
                    "default": "pending",
                },
                "limit": {
                    "type": "integer",
                    "description": "Максимум напоминалок. По умолчанию 30.",
                    "default": 30,
                },
            },
            "required": [],
        },
    },
]