# Waiter Note AI Bot (Jarvis) — Phase 1 MVP

Telegram-бот-ассистент официанта. Фаза 1: чистый чат/голос/фото
без function calling. Function calling добавим в Фазе 2.

## Стек

- Python 3.12 + aiogram 3
- Anthropic Claude Haiku 4.5 — чат и vision
- Groq Whisper large-v3 — распознавание голоса
- Postgres — история чата (та же БД что у Waiter Note backend)
- Railway — хостинг

## Локальный запуск

```bash
cp .env.example .env
# заполни .env — токены и DATABASE_URL

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# один раз — накатить миграцию
psql $DATABASE_URL -f migrations/001_ai_chat_history.sql

python main.py
```

## Деплой на Railway

1. Создай новый Railway service, `Deploy from GitHub repo`.
2. Environment variables — задать всё из `.env.example`.
   `DATABASE_URL` — тот же что у основного Waiter Note backend.
3. Один раз выполни миграцию через `railway run psql $DATABASE_URL -f migrations/001_ai_chat_history.sql`.
4. Service стартанёт `python main.py`, polling начнётся автоматически.

Не забудь **отключить polling у main WNReact backend** если он там был.
Иначе оба процесса будут ловить одни и те же updates и мешать друг другу.
Обычно у main API вообще нет polling, только webhook, но проверь.

## Команды бота

- `/start`, `/help` — приветствие
- `/clear` — очистить историю чата

## Что дальше — Phase 2

- Function calling: Claude сможет реально дёргать бэкенд Waiter Note
  через типизированные tools
- Read-only tools первыми: `list_workplaces`, `list_tables`,
  `current_shift_status`, `list_active_orders`, `list_notes`
- Аутентификация: `telegram_id` → `user_id` через существующую таблицу
  юзеров Waiter Note
- Rate limiter: 50 tool-вызовов в час на юзера

## Что дальше — Phase 3

- Write tools с подтверждениями: `create_reminder`, `switch_workplace`,
  `open_shift`, `add_menu_item`
- PDF отчёты через reportlab: `generate_monthly_report`

## Стоимость (напоминание)

- Voice (Groq): $0.00033/мин
- Chat (Haiku + caching): ~$0.001 за запрос
- Vision (Haiku): ~$0.0004 за фото

Ожидаемо ~$0.7/юзер/мес при подписке $10 = маржа 93%.
