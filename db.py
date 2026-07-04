"""Async Postgres wrapper. Один pool на весь процесс бота.

Схему для истории чата создаём миграцией 001_ai_chat_history.sql —
её надо один раз применить к существующей БД Waiter Note.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import asyncpg

from config import settings

log = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None


async def init_pool() -> None:
    """Создать connection pool. Вызывается один раз при старте бота."""
    global _pool
    if _pool is not None:
        return
    _pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=1,
        max_size=10,
        # Railway TLS требует ssl='require'; asyncpg сам разберётся
        # по префиксу postgresql:// vs postgres://
    )
    log.info("Postgres pool initialised")


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def _require_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call init_pool() first")
    return _pool


# ── История AI-чата ─────────────────────────────────────────────────


async def save_message(
    telegram_id: int,
    role: str,           # 'user' | 'assistant'
    content_type: str,   # 'text' | 'voice' | 'photo'
    content: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Записать сообщение в историю AI-чата."""
    pool = _require_pool()
    await pool.execute(
        """
        INSERT INTO ai_chat_history
            (telegram_id, role, content_type, content, metadata)
        VALUES ($1, $2, $3, $4, $5)
        """,
        telegram_id,
        role,
        content_type,
        content,
        json.dumps(metadata) if metadata else None,
    )


async def load_recent_history(
    telegram_id: int,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Вернуть последние N сообщений в хронологическом порядке
    (старые → новые), готовые к подаче в Claude.

    Формат совпадает с messages array в Anthropic API:
        [{"role": "user", "content": "..."}, ...]
    """
    pool = _require_pool()
    rows = await pool.fetch(
        """
        SELECT role, content_type, content, metadata
        FROM ai_chat_history
        WHERE telegram_id = $1
        ORDER BY created_at DESC
        LIMIT $2
        """,
        telegram_id,
        limit,
    )
    # reverse чтобы получить хронологический порядок
    rows = list(reversed(rows))
    messages: list[dict[str, Any]] = []
    for r in rows:
        messages.append({"role": r["role"], "content": r["content"]})
    return messages


async def clear_history(telegram_id: int) -> int:
    """Удалить всю историю юзера. Возвращает количество удалённых записей."""
    pool = _require_pool()
    result = await pool.execute(
        "DELETE FROM ai_chat_history WHERE telegram_id = $1",
        telegram_id,
    )
    # execute возвращает строку вида "DELETE 5"
    try:
        return int(result.split()[-1])
    except (IndexError, ValueError):
        return 0
