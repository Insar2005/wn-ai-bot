-- Таблица истории AI-чата для бота Джарвиса.
-- Применить один раз к существующей БД Waiter Note (Railway Postgres):
--   psql $DATABASE_URL -f migrations/001_ai_chat_history.sql

CREATE TABLE IF NOT EXISTS ai_chat_history (
    id            BIGSERIAL PRIMARY KEY,
    telegram_id   BIGINT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content_type  TEXT NOT NULL CHECK (content_type IN ('text', 'voice', 'photo')),
    content       TEXT NOT NULL,
    metadata      JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Основной индекс: выборка последних N сообщений юзера
CREATE INDEX IF NOT EXISTS ix_ai_chat_history_telegram_id_created
    ON ai_chat_history (telegram_id, created_at DESC);
