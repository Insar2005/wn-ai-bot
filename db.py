"""Async Postgres wrapper. Один pool на весь процесс бота.

Схема истории AI-чата (ai_chat_history) создаётся автоматически при
старте бота через ensure_schema(). Остальные таблицы (users, orders,
shifts и т.д.) уже есть — их поддерживает WNReact backend.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import asyncpg

from config import settings

log = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ai_chat_history (
    id            BIGSERIAL PRIMARY KEY,
    telegram_id   BIGINT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content_type  TEXT NOT NULL CHECK (content_type IN ('text', 'voice', 'photo')),
    content       TEXT NOT NULL,
    metadata      JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_ai_chat_history_telegram_id_created
    ON ai_chat_history (telegram_id, created_at DESC);
"""


async def init_pool() -> None:
    """Создать connection pool + накатить схему."""
    global _pool
    if _pool is not None:
        return
    _pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=1,
        max_size=10,
    )
    log.info("Postgres pool initialised")

    async with _pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
    log.info("Schema ensured (ai_chat_history)")


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    """Публичный accessor к pool. Использует tools/impl.py."""
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call init_pool() first")
    return _pool


# ── История AI-чата ─────────────────────────────────────────────────


async def save_message(
    telegram_id: int,
    role: str,
    content_type: str,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    pool = get_pool()
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
    pool = get_pool()
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
    rows = list(reversed(rows))
    messages: list[dict[str, Any]] = []
    for r in rows:
        messages.append({"role": r["role"], "content": r["content"]})
    return messages


async def clear_history(telegram_id: int) -> int:
    pool = get_pool()
    result = await pool.execute(
        "DELETE FROM ai_chat_history WHERE telegram_id = $1",
        telegram_id,
    )
    try:
        return int(result.split()[-1])
    except (IndexError, ValueError):
        return 0
