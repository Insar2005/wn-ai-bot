"""Реальные SQL-функции для tools Кибер Шефа.

Все они read-only — SELECT и JOIN, никаких INSERT/UPDATE/DELETE.
Работают через тот же asyncpg pool что и история чата (см. db.py).

Модели соответствуют backend WNReact:
  - users(id BIGINT PK, tg_id BIGINT UNIQUE, last_workplace_id str, ...)
  - workplaces(id str21 PK, owner_id BIGINT, title, is_archived, ...)
  - workplace_members(workplace_id, user_id, role)
  - shifts(id str21, workplace_id, opened_by_user_id, start_time BIGINT,
           end_time BIGINT nullable, is_closed, totals...)
  - orders(id str21, shift_id, table_id, table_number, total_price,
           tips, is_paid, is_done, guests_count, ...)
  - order_items(id str21, order_id, title, price, quantity,
                total_price, comment, served, guest)
  - halls(id str21, workplace_id, name, ...)
  - tables(id str21, hall_id, order_id nullable, number, status)
  - menu_categories(id str21, workplace_id, title, is_active)
  - menu_items(id str21, category_id, title, description, portion,
               price, is_active)
  - notes(id str21, user_id, scope, workplace_id?, shift_id?,
          header, content, pinned, is_archived)
  - reminders(id str21, user_id, text, remind_at BIGINT,
              lead_minutes, is_done, notified_at)

Timestamps в БД — Unix seconds (BIGINT). Мы конвертируем в человеческий
формат (isoformat + hint "5 мин назад") перед отдачей Claude.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from db import get_pool

log = logging.getLogger(__name__)


# ── Helpers ─────────────────────────────────────────────────────────


def _fmt_ts(ts: Optional[int]) -> Optional[str]:
    """Unix seconds → 'YYYY-MM-DD HH:MM' UTC. None → None."""
    if ts is None:
        return None
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _hint_ago(ts: Optional[int]) -> Optional[str]:
    """Человеческий hint: '5 мин назад', '2 часа назад', '3 дня назад'."""
    if ts is None:
        return None
    now = int(datetime.now(tz=timezone.utc).timestamp())
    diff = now - ts
    if diff < 0:
        # В будущем — например remind_at
        diff = -diff
        if diff < 60:
            return f"через {diff} сек"
        if diff < 3600:
            return f"через {diff // 60} мин"
        if diff < 86400:
            return f"через {diff // 3600} ч"
        return f"через {diff // 86400} дн"
    if diff < 60:
        return f"{diff} сек назад"
    if diff < 3600:
        return f"{diff // 60} мин назад"
    if diff < 86400:
        return f"{diff // 3600} ч назад"
    return f"{diff // 86400} дн назад"


def _hhmm_from_seconds(seconds: int) -> str:
    """1234 сек → '20м 34с'. 3660 → '1ч 1м'. 90000 → '25ч 0м'."""
    if seconds < 60:
        return f"{seconds}с"
    if seconds < 3600:
        return f"{seconds // 60}м {seconds % 60}с"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours}ч {minutes}м"


# ── Auth: telegram_id → user_id + active workplace ─────────────────


async def resolve_user(tg_id: int) -> Optional[dict[str, Any]]:
    """Найти юзера по telegram_id. Возвращает базовый профиль или None
    если юзер ещё не заводил аккаунт в Waiter Note.
    """
    pool = get_pool()
    row = await pool.fetchrow(
        """
        SELECT id, tg_id, username, language, timezone,
               last_workplace_id, is_disabled
        FROM users
        WHERE tg_id = $1
        """,
        tg_id,
    )
    if row is None:
        return None
    return {
        "user_id": row["id"],
        "tg_id": row["tg_id"],
        "username": row["username"],
        "language": row["language"],
        "timezone": row["timezone"],
        "current_workplace_id": row["last_workplace_id"],
        "is_disabled": row["is_disabled"],
    }


# ── Tools ───────────────────────────────────────────────────────────


async def get_me(user_id: int) -> dict[str, Any]:
    """Инфо о самом юзере — язык, таймзона, активное заведение."""
    pool = get_pool()
    row = await pool.fetchrow(
        """
        SELECT u.id, u.tg_id, u.username, u.language, u.timezone,
               u.last_workplace_id, w.title AS current_workplace_title
        FROM users u
        LEFT JOIN workplaces w ON w.id = u.last_workplace_id
        WHERE u.id = $1
        """,
        user_id,
    )
    if row is None:
        return {"error": "user_not_found"}
    return dict(row)


async def list_workplaces(user_id: int) -> list[dict[str, Any]]:
    """Все заведения куда юзер имеет доступ (owner или member)."""
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT w.id, w.title, w.currency, w.timezone,
               w.is_archived, wm.role
        FROM workplaces w
        JOIN workplace_members wm ON wm.workplace_id = w.id
        WHERE wm.user_id = $1
        ORDER BY w.is_archived, w.position, w.title
        """,
        user_id,
    )
    return [dict(r) for r in rows]


async def get_current_shift(
    user_id: int,
    workplace_id: Optional[str] = None,
) -> dict[str, Any]:
    """Открытая смена юзера в указанном заведении (или в активном по
    умолчанию). Даёт длительность, заработано, чаевые, кол-во заказов.
    """
    pool = get_pool()

    if workplace_id is None:
        # Взять активное заведение из users.last_workplace_id
        workplace_id = await pool.fetchval(
            "SELECT last_workplace_id FROM users WHERE id = $1", user_id
        )
        if workplace_id is None:
            return {"error": "no_active_workplace"}

    row = await pool.fetchrow(
        """
        SELECT s.id, s.workplace_id, s.start_time, s.end_time,
               s.is_closed, s.place_work_title, s.currency,
               s.shift_type, s.pay_for_shift,
               s.total_pay_for_shift, s.total_tips,
               s.total_cash_register, s.order_count
        FROM shifts s
        WHERE s.workplace_id = $1
          AND s.opened_by_user_id = $2
          AND s.end_time IS NULL
        ORDER BY s.start_time DESC
        LIMIT 1
        """,
        workplace_id,
        user_id,
    )
    if row is None:
        return {"status": "no_open_shift", "workplace_id": workplace_id}

    now = int(datetime.now(tz=timezone.utc).timestamp())
    duration_sec = now - row["start_time"]

    return {
        "status": "open",
        "shift_id": row["id"],
        "workplace_id": row["workplace_id"],
        "workplace_title": row["place_work_title"],
        "started_at": _fmt_ts(row["start_time"]),
        "started_ago": _hint_ago(row["start_time"]),
        "duration": _hhmm_from_seconds(duration_sec),
        "currency": row["currency"],
        "shift_type": row["shift_type"],
        "pay_for_shift": float(row["pay_for_shift"]),
        "earned_total": float(row["total_pay_for_shift"]),
        "tips_total": float(row["total_tips"]),
        "cash_register_total": float(row["total_cash_register"]),
        "order_count": row["order_count"],
    }


async def list_recent_shifts(
    user_id: int,
    limit: int = 10,
    workplace_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Последние закрытые смены юзера. Если workplace_id не задан —
    по всем заведениям к которым у юзера доступ."""
    pool = get_pool()
    if workplace_id:
        rows = await pool.fetch(
            """
            SELECT id, workplace_id, place_work_title, start_time,
                   end_time, total_pay_for_shift, total_tips,
                   order_count, duration
            FROM shifts
            WHERE opened_by_user_id = $1
              AND workplace_id = $2
              AND end_time IS NOT NULL
            ORDER BY start_time DESC
            LIMIT $3
            """,
            user_id,
            workplace_id,
            limit,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT id, workplace_id, place_work_title, start_time,
                   end_time, total_pay_for_shift, total_tips,
                   order_count, duration
            FROM shifts
            WHERE opened_by_user_id = $1
              AND end_time IS NOT NULL
            ORDER BY start_time DESC
            LIMIT $2
            """,
            user_id,
            limit,
        )
    return [
        {
            "shift_id": r["id"],
            "workplace_id": r["workplace_id"],
            "workplace_title": r["place_work_title"],
            "started_at": _fmt_ts(r["start_time"]),
            "ended_at": _fmt_ts(r["end_time"]),
            "duration": _hhmm_from_seconds(r["duration"]),
            "earned": float(r["total_pay_for_shift"]),
            "tips": float(r["total_tips"]),
            "orders": r["order_count"],
        }
        for r in rows
    ]


async def list_active_orders(
    user_id: int,
    workplace_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Открытые (не оплаченные) заказы в активной смене юзера."""
    pool = get_pool()

    if workplace_id is None:
        workplace_id = await pool.fetchval(
            "SELECT last_workplace_id FROM users WHERE id = $1", user_id
        )
        if workplace_id is None:
            return []

    rows = await pool.fetch(
        """
        SELECT o.id, o.table_number, o.hall_name, o.total_price,
               o.tips, o.guests_count, o.created_at, o.is_done,
               (SELECT COUNT(*) FROM order_items oi WHERE oi.order_id = o.id)
                   AS items_count,
               (SELECT COUNT(*) FROM order_items oi
                    WHERE oi.order_id = o.id AND oi.served < oi.quantity)
                   AS unserved_count
        FROM orders o
        JOIN shifts s ON s.id = o.shift_id
        WHERE s.workplace_id = $1
          AND s.opened_by_user_id = $2
          AND s.end_time IS NULL
          AND o.is_paid = FALSE
        ORDER BY o.created_at DESC
        """,
        workplace_id,
        user_id,
    )
    return [
        {
            "order_id": r["id"],
            "table": (
                f"стол №{r['table_number']}"
                if r["table_number"] is not None
                else "без стола"
            ),
            "hall": r["hall_name"],
            "guests": r["guests_count"],
            "total_price": float(r["total_price"]),
            "tips": float(r["tips"]),
            "items_count": r["items_count"],
            "unserved_count": r["unserved_count"],
            "created_ago": _hint_ago(r["created_at"]),
            "is_done": r["is_done"],
        }
        for r in rows
    ]


async def get_order(user_id: int, order_id: str) -> dict[str, Any]:
    """Детали заказа: сам заказ + все позиции. Проверяет что заказ
    принадлежит смене юзера — иначе `error: not_found`."""
    pool = get_pool()
    order = await pool.fetchrow(
        """
        SELECT o.id, o.table_number, o.hall_name, o.total_price, o.tips,
               o.guests_count, o.comments, o.created_at, o.closed_at,
               o.is_paid, o.is_done
        FROM orders o
        JOIN shifts s ON s.id = o.shift_id
        WHERE o.id = $1
          AND s.opened_by_user_id = $2
        """,
        order_id,
        user_id,
    )
    if order is None:
        return {"error": "not_found"}

    items = await pool.fetch(
        """
        SELECT id, title, price, quantity, total_price, comment, served, guest
        FROM order_items
        WHERE order_id = $1
        ORDER BY guest, id
        """,
        order_id,
    )
    return {
        "order_id": order["id"],
        "table": (
            f"стол №{order['table_number']}"
            if order["table_number"] is not None
            else "без стола"
        ),
        "hall": order["hall_name"],
        "guests": order["guests_count"],
        "total_price": float(order["total_price"]),
        "tips": float(order["tips"]),
        "comments": order["comments"],
        "created_at": _fmt_ts(order["created_at"]),
        "closed_at": _fmt_ts(order["closed_at"]),
        "is_paid": order["is_paid"],
        "is_done": order["is_done"],
        "items": [
            {
                "title": i["title"],
                "price": float(i["price"]),
                "quantity": i["quantity"],
                "total": float(i["total_price"]),
                "comment": i["comment"],
                # счётчик поданных штук: 0..quantity
                "served": i["served"],
                "served_full": i["served"] >= i["quantity"],
                "guest": i["guest"],
            }
            for i in items
        ],
    }


async def list_halls(
    user_id: int,
    workplace_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Залы в заведении."""
    pool = get_pool()
    if workplace_id is None:
        workplace_id = await pool.fetchval(
            "SELECT last_workplace_id FROM users WHERE id = $1", user_id
        )
        if workplace_id is None:
            return []

    # Проверяем что юзер имеет доступ к workplace
    has_access = await pool.fetchval(
        """
        SELECT 1 FROM workplace_members
        WHERE workplace_id = $1 AND user_id = $2
        """,
        workplace_id,
        user_id,
    )
    if not has_access:
        return []

    rows = await pool.fetch(
        """
        SELECT h.id, h.name, h.position,
               (SELECT COUNT(*) FROM tables t WHERE t.hall_id = h.id)
                   AS tables_count
        FROM halls h
        WHERE h.workplace_id = $1
        ORDER BY h.position, h.name
        """,
        workplace_id,
    )
    return [dict(r) for r in rows]


async def list_tables(
    user_id: int,
    hall_id: Optional[str] = None,
    only_free: bool = False,
) -> list[dict[str, Any]]:
    """Столы в зале. Если hall_id не задан — все столы всех залов
    активного заведения. only_free=True — фильтр по status='free'.
    """
    pool = get_pool()

    if hall_id is None:
        workplace_id = await pool.fetchval(
            "SELECT last_workplace_id FROM users WHERE id = $1", user_id
        )
        if workplace_id is None:
            return []
        # Все столы всех залов workplace
        query = """
            SELECT t.id, t.number, t.status, t.order_id,
                   h.id AS hall_id, h.name AS hall_name
            FROM tables t
            JOIN halls h ON h.id = t.hall_id
            WHERE h.workplace_id = $1
        """
        params: list[Any] = [workplace_id]
        if only_free:
            query += " AND t.status = 'free'"
        query += " ORDER BY h.position, t.number"
        rows = await pool.fetch(query, *params)
    else:
        query = """
            SELECT t.id, t.number, t.status, t.order_id,
                   h.id AS hall_id, h.name AS hall_name
            FROM tables t
            JOIN halls h ON h.id = t.hall_id
            WHERE t.hall_id = $1
        """
        params = [hall_id]
        if only_free:
            query += " AND t.status = 'free'"
        query += " ORDER BY t.number"
        rows = await pool.fetch(query, *params)

    return [
        {
            "table_id": r["id"],
            "number": r["number"],
            "status": r["status"],
            "hall_id": r["hall_id"],
            "hall_name": r["hall_name"],
            "has_order": r["order_id"] is not None,
        }
        for r in rows
    ]


async def search_menu(
    user_id: int,
    query: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Поиск по меню активного заведения. query — подстрока (ILIKE),
    category — точное название категории (ILIKE match).
    Если ни то ни другое не задано — вернёт первые limit позиций.
    """
    pool = get_pool()
    workplace_id = await pool.fetchval(
        "SELECT last_workplace_id FROM users WHERE id = $1", user_id
    )
    if workplace_id is None:
        return []

    conditions = [
        "mc.workplace_id = $1",
        "mc.is_active = TRUE",
        "mi.is_active = TRUE",
    ]
    params: list[Any] = [workplace_id]

    if query:
        params.append(f"%{query}%")
        conditions.append(f"(mi.title ILIKE ${len(params)} "
                          f"OR mi.description ILIKE ${len(params)})")
    if category:
        params.append(f"%{category}%")
        conditions.append(f"mc.title ILIKE ${len(params)}")

    params.append(limit)
    sql = f"""
        SELECT mi.id, mi.title, mi.description, mi.portion, mi.price,
       mi.comment_chips, mc.title AS category
FROM menu_items mi
JOIN menu_categories mc ON mc.id = mi.category_id
WHERE {" AND ".join(conditions)}
ORDER BY mc.position, mi.position, mi.title
LIMIT ${len(params)}
    """
    rows = await pool.fetch(sql, *params)
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "description": r["description"],
            "portion": r["portion"],
            "price": float(r["price"]),
            "comment_chips": r["comment_chips"] or [],  # jsonb → list
            "category": r["category"],
        }
        for r in rows
    ]


async def list_menu_categories(
    user_id: int,
) -> list[dict[str, Any]]:
    """Категории меню активного заведения с кол-вом позиций."""
    pool = get_pool()
    workplace_id = await pool.fetchval(
        "SELECT last_workplace_id FROM users WHERE id = $1", user_id
    )
    if workplace_id is None:
        return []

    rows = await pool.fetch(
        """
        SELECT mc.id, mc.title, mc.position, mc.parent_id,
       (SELECT COUNT(*) FROM menu_items mi
        WHERE mi.category_id = mc.id AND mi.is_active = TRUE)
            AS items_count
FROM menu_categories mc
WHERE mc.workplace_id = $1 AND mc.is_active = TRUE
ORDER BY mc.position, mc.title
        """,
        workplace_id,
    )
    return [dict(r) for r in rows]


async def list_notes(
    user_id: int,
    query: Optional[str] = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Заметки юзера. query — подстрока по header/content (ILIKE)."""
    pool = get_pool()
    conditions = ["user_id = $1", "is_archived = FALSE"]
    params: list[Any] = [user_id]

    if query:
        params.append(f"%{query}%")
        conditions.append(
            f"(header ILIKE ${len(params)} OR content ILIKE ${len(params)})"
        )

    params.append(limit)
    sql = f"""
        SELECT id, scope, workplace_id, shift_id, header, content,
               pinned, created_at, updated_at
        FROM notes
        WHERE {" AND ".join(conditions)}
        ORDER BY pinned DESC, updated_at DESC
        LIMIT ${len(params)}
    """
    rows = await pool.fetch(sql, *params)
    return [
        {
            "id": r["id"],
            "scope": r["scope"],
            "header": r["header"],
            "content": r["content"],
            "pinned": r["pinned"],
            "updated_ago": _hint_ago(r["updated_at"]),
        }
        for r in rows
    ]


async def list_reminders(
    user_id: int,
    when: str = "pending",     # 'today' | 'tomorrow' | 'pending' | 'overdue' | 'all'
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Напоминалки юзера. when:
      today    — сегодня (00:00 — 23:59 в UTC от текущего дня)
      tomorrow — завтра (те же 24 часа завтра)
      pending  — все не выполненные (is_done=false)
      overdue  — просроченные (remind_at < now, is_done=false)
      all      — вообще все, включая выполненные
    """
    pool = get_pool()
    now = int(datetime.now(tz=timezone.utc).timestamp())

    conditions = ["user_id = $1"]
    params: list[Any] = [user_id]

    if when == "today":
        # Сегодня 00:00 UTC до 23:59:59
        today_start = now - (now % 86400)
        today_end = today_start + 86399
        params.extend([today_start, today_end])
        conditions.append(
            f"remind_at BETWEEN ${len(params)-1} AND ${len(params)}"
        )
    elif when == "tomorrow":
        tomorrow_start = now - (now % 86400) + 86400
        tomorrow_end = tomorrow_start + 86399
        params.extend([tomorrow_start, tomorrow_end])
        conditions.append(
            f"remind_at BETWEEN ${len(params)-1} AND ${len(params)}"
        )
    elif when == "pending":
        conditions.append("is_done = FALSE")
    elif when == "overdue":
        params.append(now)
        conditions.append(f"is_done = FALSE AND remind_at < ${len(params)}")
    # 'all' — без фильтра

    params.append(limit)
    sql = f"""
        SELECT id, text, remind_at, lead_minutes, is_done, notified_at,
               created_at
        FROM reminders
        WHERE {" AND ".join(conditions)}
        ORDER BY remind_at ASC
        LIMIT ${len(params)}
    """
    rows = await pool.fetch(sql, *params)
    return [
        {
            "id": r["id"],
            "text": r["text"],
            "remind_at": _fmt_ts(r["remind_at"]),
            "when_hint": _hint_ago(r["remind_at"]),
            "lead_minutes": r["lead_minutes"],
            "is_done": r["is_done"],
            "notified": r["notified_at"] is not None,
        }
        for r in rows
    ]


# ═══════════════════════════════════════════════════════════════════
# Phase 3: запись в меню (импорт с фото, описания, раскладка)
# ═══════════════════════════════════════════════════════════════════

_NANO_ALPHABET = "useandom-26T198340PX75pxJACKVERYMINDBUSHWOLF_GQZbfghjklqvwyzrict"


def _nanoid(size: int = 21) -> str:
    """Nanoid, совместимый с фронтом/бэком (21 символ, тот же алфавит)."""
    import secrets

    return "".join(secrets.choice(_NANO_ALPHABET) for _ in range(size))


async def _active_workplace_checked(user_id: int) -> Optional[str]:
    """last_workplace_id юзера + проверка членства. None = нет доступа."""
    pool = get_pool()
    workplace_id = await pool.fetchval(
        "SELECT last_workplace_id FROM users WHERE id = $1", user_id
    )
    if workplace_id is None:
        return None
    has_access = await pool.fetchval(
        """
        SELECT 1 FROM workplace_members
        WHERE workplace_id = $1 AND user_id = $2
        """,
        workplace_id,
        user_id,
    )
    return workplace_id if has_access else None


async def _category_in_workplace(category_id: str, workplace_id: str):
    pool = get_pool()
    return await pool.fetchrow(
        """
        SELECT id, title, parent_id FROM menu_categories
        WHERE id = $1 AND workplace_id = $2
        """,
        category_id,
        workplace_id,
    )


async def list_menu_items(
    user_id: int,
    category_id: Optional[str] = None,
    limit: int = 300,
) -> list[dict[str, Any]]:
    """Полный список позиций меню (для раскладки по категориям и ревизии
    описаний). category_id — опциональный фильтр."""
    pool = get_pool()
    workplace_id = await _active_workplace_checked(user_id)
    if workplace_id is None:
        return []

    params: list[Any] = [workplace_id]
    cond = "mc.workplace_id = $1 AND mi.is_active = TRUE AND mc.is_active = TRUE"
    if category_id:
        params.append(category_id)
        cond += f" AND mi.category_id = ${len(params)}"
    params.append(min(int(limit), 500))
    rows = await pool.fetch(
        f"""
        SELECT mi.id, mi.title, mi.price, mi.portion, mi.description,
               mi.category_id, mc.title AS category, mc.parent_id AS category_parent_id
        FROM menu_items mi
        JOIN menu_categories mc ON mc.id = mi.category_id
        WHERE {cond}
        ORDER BY mc.position, mi.position, mi.title
        LIMIT ${len(params)}
        """,
        *params,
    )
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "price": float(r["price"]),
            "portion": r["portion"],
            "has_description": bool(r["description"]),
            "description": r["description"],
            "category_id": r["category_id"],
            "category": r["category"],
        }
        for r in rows
    ]


async def create_menu_category(
    user_id: int,
    title: str,
    parent_id: Optional[str] = None,
) -> dict[str, Any]:
    """Создать категорию (или подкатегорию, если задан parent_id)."""
    pool = get_pool()
    workplace_id = await _active_workplace_checked(user_id)
    if workplace_id is None:
        return {"error": "no_active_workplace"}
    title = (title or "").strip()
    if not title:
        return {"error": "empty_title"}
    if parent_id:
        parent = await _category_in_workplace(parent_id, workplace_id)
        if parent is None:
            return {"error": "parent_not_found"}

    cat_id = _nanoid()
    max_pos = await pool.fetchval(
        """
        SELECT COALESCE(MAX(position), -1) FROM menu_categories
        WHERE workplace_id = $1
          AND parent_id IS NOT DISTINCT FROM $2
        """,
        workplace_id,
        parent_id,
    )
    await pool.execute(
        """
        INSERT INTO menu_categories (id, workplace_id, title, parent_id, position, is_active)
        VALUES ($1, $2, $3, $4, $5, TRUE)
        """,
        cat_id,
        workplace_id,
        title[:100],
        parent_id,
        (max_pos or 0) + 1,
    )
    return {"id": cat_id, "title": title, "parent_id": parent_id}


async def update_menu_category(
    user_id: int,
    category_id: str,
    title: Optional[str] = None,
    parent_id: Optional[str] = None,
) -> dict[str, Any]:
    """Переименовать категорию и/или переместить. parent_id: не передан —
    не трогаем; "" (пустая строка) — сделать корневой; id — новый родитель.
    Защита от циклов (категория в собственное поддерево)."""
    pool = get_pool()
    workplace_id = await _active_workplace_checked(user_id)
    if workplace_id is None:
        return {"error": "no_active_workplace"}
    cat = await _category_in_workplace(category_id, workplace_id)
    if cat is None:
        return {"error": "category_not_found"}

    sets: list[str] = []
    params: list[Any] = []
    if title is not None and title.strip():
        params.append(title.strip()[:100])
        sets.append(f"title = ${len(params)}")

    if parent_id is not None:
        new_parent = None if parent_id == "" else parent_id
        if new_parent == category_id:
            return {"error": "cannot_parent_self"}
        if new_parent:
            parent = await _category_in_workplace(new_parent, workplace_id)
            if parent is None:
                return {"error": "parent_not_found"}
            # подъём по цепочке: цикл?
            cursor = parent
            seen: set[str] = set()
            while cursor is not None and cursor["parent_id"]:
                if cursor["parent_id"] == category_id:
                    return {"error": "would_create_cycle"}
                if cursor["parent_id"] in seen:
                    break
                seen.add(cursor["parent_id"])
                cursor = await _category_in_workplace(
                    cursor["parent_id"], workplace_id
                )
        params.append(new_parent)
        sets.append(f"parent_id = ${len(params)}")

    if not sets:
        return {"error": "nothing_to_update"}
    params.append(category_id)
    await pool.execute(
        f"UPDATE menu_categories SET {', '.join(sets)} WHERE id = ${len(params)}",
        *params,
    )
    return {"ok": True, "id": category_id}


async def create_menu_items(
    user_id: int,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Батч-создание позиций (импорт меню). Каждый item:
    {category_id, title, price, portion?, description?}. До 60 за вызов."""
    pool = get_pool()
    workplace_id = await _active_workplace_checked(user_id)
    if workplace_id is None:
        return {"error": "no_active_workplace"}
    if not items:
        return {"error": "empty_items"}
    if len(items) > 60:
        return {"error": "too_many_items", "max": 60}

    # валидация категорий одним заходом
    cat_ids = {str(i.get("category_id") or "") for i in items}
    if "" in cat_ids:
        return {"error": "item_without_category"}
    rows = await pool.fetch(
        """
        SELECT id FROM menu_categories
        WHERE workplace_id = $1 AND id = ANY($2::varchar[])
        """,
        workplace_id,
        list(cat_ids),
    )
    found = {r["id"] for r in rows}
    missing = cat_ids - found
    if missing:
        return {"error": "category_not_found", "ids": sorted(missing)}

    # позиции: продолжаем нумерацию в каждой категории
    pos_rows = await pool.fetch(
        """
        SELECT category_id, COALESCE(MAX(position), -1) AS mx
        FROM menu_items WHERE category_id = ANY($1::varchar[])
        GROUP BY category_id
        """,
        list(cat_ids),
    )
    next_pos = {r["category_id"]: r["mx"] + 1 for r in pos_rows}

    created: list[dict[str, Any]] = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            for raw in items:
                cid = str(raw["category_id"])
                title = str(raw.get("title") or "").strip()[:150]
                if not title:
                    continue
                try:
                    price = round(float(raw.get("price") or 0), 2)
                except (TypeError, ValueError):
                    price = 0.0
                pos = next_pos.get(cid, 0)
                next_pos[cid] = pos + 1
                item_id = _nanoid()
                await conn.execute(
                    """
                    INSERT INTO menu_items
                        (id, category_id, title, description, portion, price,
                         comment_chips, position, is_active)
                    VALUES ($1, $2, $3, $4, $5, $6, '[]'::jsonb, $7, TRUE)
                    """,
                    item_id,
                    cid,
                    title,
                    (str(raw.get("description")).strip()[:2000]
                     if raw.get("description") else None),
                    (str(raw.get("portion")).strip()[:50]
                     if raw.get("portion") else None),
                    price,
                    pos,
                )
                created.append({"id": item_id, "title": title, "price": price})
    return {"ok": True, "created_count": len(created), "created": created}


async def update_menu_item(
    user_id: int,
    item_id: str,
    title: Optional[str] = None,
    price: Optional[float] = None,
    portion: Optional[str] = None,
    description: Optional[str] = None,
    category_id: Optional[str] = None,
) -> dict[str, Any]:
    """Правка позиции: описание, цена, порция, название, перенос в другую
    категорию (category_id)."""
    pool = get_pool()
    workplace_id = await _active_workplace_checked(user_id)
    if workplace_id is None:
        return {"error": "no_active_workplace"}
    row = await pool.fetchrow(
        """
        SELECT mi.id FROM menu_items mi
        JOIN menu_categories mc ON mc.id = mi.category_id
        WHERE mi.id = $1 AND mc.workplace_id = $2
        """,
        item_id,
        workplace_id,
    )
    if row is None:
        return {"error": "item_not_found"}

    sets: list[str] = []
    params: list[Any] = []

    def add(field: str, value: Any) -> None:
        params.append(value)
        sets.append(f"{field} = ${len(params)}")

    if title is not None and title.strip():
        add("title", title.strip()[:150])
    if price is not None:
        try:
            add("price", round(float(price), 2))
        except (TypeError, ValueError):
            return {"error": "bad_price"}
    if portion is not None:
        add("portion", portion.strip()[:50] or None)
    if description is not None:
        add("description", description.strip()[:2000] or None)
    if category_id is not None:
        target = await _category_in_workplace(category_id, workplace_id)
        if target is None:
            return {"error": "category_not_found"}
        new_pos = await pool.fetchval(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM menu_items WHERE category_id = $1",
            category_id,
        )
        add("category_id", category_id)
        add("position", new_pos)

    if not sets:
        return {"error": "nothing_to_update"}
    params.append(item_id)
    await pool.execute(
        f"UPDATE menu_items SET {', '.join(sets)} WHERE id = ${len(params)}",
        *params,
    )
    return {"ok": True, "id": item_id}


async def delete_menu_category(
    user_id: int,
    category_ids,
) -> dict[str, Any]:
    """Удалить одну или несколько категорий ВМЕСТЕ с подкатегориями и
    всеми позициями внутри. category_ids: список id (или один id
    строкой). Вызывается только после подтверждения плана юзером."""
    pool = get_pool()
    workplace_id = await _active_workplace_checked(user_id)
    if workplace_id is None:
        return {"error": "no_active_workplace"}

    if isinstance(category_ids, str):
        category_ids = [category_ids]
    category_ids = [str(c) for c in (category_ids or []) if c]
    if not category_ids:
        return {"error": "empty_category_ids"}
    if len(category_ids) > 40:
        return {"error": "too_many_categories", "max": 40}

    deleted: list[dict[str, Any]] = []
    not_found: list[str] = []
    wiped: set[str] = set()  # уже снесённые (напр. как подкатегория предыдущей)

    for category_id in category_ids:
        if category_id in wiped:
            continue
        root = await _category_in_workplace(category_id, workplace_id)
        if root is None:
            not_found.append(category_id)
            continue

        # поддерево (BFS с защитой от битых циклов в данных)
        ids: list[str] = [category_id]
        seen: set[str] = {category_id}
        frontier = [category_id]
        while frontier:
            rows = await pool.fetch(
                """
                SELECT id FROM menu_categories
                WHERE workplace_id = $1 AND parent_id = ANY($2::varchar[])
                """,
                workplace_id,
                frontier,
            )
            frontier = [r["id"] for r in rows if r["id"] not in seen]
            seen.update(frontier)
            ids.extend(frontier)

        async with pool.acquire() as conn:
            async with conn.transaction():
                items_deleted = await conn.fetchval(
                    """
                    WITH gone AS (
                        DELETE FROM menu_items
                        WHERE category_id = ANY($1::varchar[])
                        RETURNING 1
                    )
                    SELECT COUNT(*) FROM gone
                    """,
                    ids,
                )
                await conn.execute(
                    "DELETE FROM menu_categories WHERE id = ANY($1::varchar[])",
                    ids,
                )
        wiped.update(ids)
        deleted.append(
            {
                "id": category_id,
                "title": root["title"],
                "categories_deleted": len(ids),
                "items_deleted": int(items_deleted or 0),
            }
        )

    out: dict[str, Any] = {"ok": True, "deleted": deleted}
    if not_found:
        out["not_found"] = not_found
    return out


async def delete_menu_items(
    user_id: int,
    item_ids,
) -> dict[str, Any]:
    """Удалить одну или несколько ПОЗИЦИЙ меню (не категорий). item_ids:
    список id (или один id строкой). Только после подтверждения плана."""
    pool = get_pool()
    workplace_id = await _active_workplace_checked(user_id)
    if workplace_id is None:
        return {"error": "no_active_workplace"}

    if isinstance(item_ids, str):
        item_ids = [item_ids]
    item_ids = [str(i) for i in (item_ids or []) if i]
    if not item_ids:
        return {"error": "empty_item_ids"}
    if len(item_ids) > 60:
        return {"error": "too_many_items", "max": 60}

    # только позиции этого заведения (join через категорию)
    rows = await pool.fetch(
        """
        SELECT mi.id, mi.title
        FROM menu_items mi
        JOIN menu_categories mc ON mc.id = mi.category_id
        WHERE mi.id = ANY($1::varchar[]) AND mc.workplace_id = $2
        """,
        item_ids,
        workplace_id,
    )
    found = {r["id"]: r["title"] for r in rows}
    not_found = [i for i in item_ids if i not in found]

    if found:
        await pool.execute(
            "DELETE FROM menu_items WHERE id = ANY($1::varchar[])",
            list(found.keys()),
        )

    out: dict[str, Any] = {
        "ok": True,
        "deleted_count": len(found),
        "deleted": [{"id": i, "title": t} for i, t in found.items()],
    }
    if not_found:
        out["not_found"] = not_found
    return out