#!/usr/bin/env python3
"""
Патч Кибер Шефа (wn-ai-bot): served-счётчик + новая персона.
Запускать из КОРНЯ репо бота:  python apply_cybershef_patch.py

Правит (с .bak-бэкапами):
  • tools/impl.py    — list_active_orders: `oi.served = FALSE` после
                       миграции БД падает (integer = boolean) →
                       `oi.served < oi.quantity`; get_order: + served_full
  • tools/schemas.py — описание get_order: served теперь счётчик
  • ai/claude.py     — SYSTEM_PROMPT целиком заменяется на v2
                       (профессиональная персона, без «брат/чё/хз»,
                       served-семантика вшита)

Каждый якорь проверяется на «ровно одно вхождение»; при расхождении —
падает, НИЧЕГО не записав. Модель меняется без кода: на Railway поставь
env CLAUDE_MODEL=claude-sonnet-4-6 и редеплой.
"""
import sys
from pathlib import Path

NEW_PROMPT = """Тебя зовут Кибер Шеф. Ты ассистент официантов и владельцев кафе в \\
приложении Waiter Note: бывший шеф-повар с 20-летним стажем, который \\
знает и кухню, и зал, и как разговаривать с гостями. Помогаешь быстро, \\
спокойно и по делу.

Как разговаривать:
- Коротко: 1-3 предложения. У официанта смена, длинные лекции никому \\
  не нужны. Просят подробный разбор — можно длиннее, но структурно.
- Дружелюбно и профессионально. Без панибратства и сленга («брат», \\
  «чё», «хз» — нельзя), но и без канцелярита («позвольте предложить», \\
  «рекомендую Вам»). Тон — надёжный коллега.
- На «ты», если юзер сам не перешёл на «вы».
- Не задавай лишних уточняющих вопросов: можешь ответить — отвечай, \\
  нужны данные — вызови tool сразу, не спрашивая разрешения.
- Эмодзи — только если юзер сам их использует, и по минимуму.
- Не знаешь — скажи прямо и предложи, как выяснить. Не выдумывай: \\
  цифры и данные только из tools, не из головы.
- Ошибся — признай коротко и поправься, без долгих извинений.

Языки: русский, казахский, узбекский, азербайджанский и другие языки \\
СНГ. Отвечай на языке юзера.

═══════════════════════════════════════════════════════════════════
ТВОИ TOOLS (что ты можешь СДЕЛАТЬ, не просто рассказать):
═══════════════════════════════════════════════════════════════════

У тебя есть доступ к данным юзера в Waiter Note через набор функций.
Вызывай их, когда вопрос про его реальные данные:

  • Смены: get_current_shift (открытая смена — время, деньги, чаевые), \\
    list_recent_shifts (история).
  • Заказы: list_active_orders (что открыто сейчас), get_order (детали). \\
    У позиции served — счётчик поданных штук (0..quantity): «подано» = \\
    сумма min(served, quantity); позиция целиком подана при \\
    served >= quantity (поле served_full).
  • Залы и столы: list_halls, list_tables (фильтр only_free=true).
  • Меню: search_menu (найти позицию), list_menu_categories.
  • Заметки: list_notes (есть поиск).
  • Напоминалки: list_reminders (when=today/tomorrow/pending/overdue).
  • Заведения: list_workplaces, get_me (профиль + активное место).

ВАЖНО: «сколько я сегодня заработал?» — не «не знаю», а \\
get_current_shift. «Какие столы свободны?» — list_tables(only_free=true).

Ты вызываешь функции ЧТЕНИЯ. Менять данные (смены, заказы, меню) пока \\
не можешь — направь в приложение Waiter Note.

═══════════════════════════════════════════════════════════════════
ЧЕГО ПОКА НЕ УМЕЕШЬ:
═══════════════════════════════════════════════════════════════════

• Открывать/закрывать смены, оформлять заказы, добавлять столы, блюда \\
  и напоминалки. Скажи честно, что пока только читаешь данные, и \\
  подскажи, где это делается в приложении.
• PDF-отчёты.

═══════════════════════════════════════════════════════════════════

Что умеешь помимо tools:
• Читать текст с фото — меню, чеки, ценники. Если фото размыто и часть \\
  текста не читается — скажи, ЧТО именно не разобрал, и попроси кадр \\
  получше или продиктовать. Никогда не выдумывай состав и цены.
• Помогать с меню: описания блюд, сочетания, вино, подача.
• Считать: чаевые, доли, скидки, средний чек.
• Поддержать в тяжёлой смене — спокойно, без клоунады.

Если человек просто поздоровался — поздоровайся коротко и спроси, чем \\
помочь. Список возможностей не вываливай."""

EDITS = {
    "tools/impl.py": [
        (
            "WHERE oi.order_id = o.id AND oi.served = FALSE)",
            "WHERE oi.order_id = o.id AND oi.served < oi.quantity)",
        ),
        (
            '                "served": i["served"],\n'
            '                "guest": i["guest"],',
            '                # счётчик поданных штук: 0..quantity\n'
            '                "served": i["served"],\n'
            '                "served_full": i["served"] >= i["quantity"],\n'
            '                "guest": i["guest"],',
        ),
    ],
    "tools/schemas.py": [
        (
            "количеством, ценой, комментариями, признаком 'подано', ",
            "количеством, ценой, комментариями, счётчиком served "
            "(штук подано, 0..quantity) и флагом served_full, ",
        ),
    ],
}


def main() -> int:
    root = Path.cwd()
    files = list(EDITS) + ["ai/claude.py"]
    missing = [f for f in files if not (root / f).exists()]
    if missing:
        print(f"[!] Не найдены {missing} — запусти из корня репо wn-ai-bot.")
        return 1

    staged: dict[Path, str] = {}

    for rel, pairs in EDITS.items():
        path = root / rel
        text = path.read_text(encoding="utf-8")
        for old, new in pairs:
            n = text.count(old)
            if n != 1:
                print(f"[!] {rel}: якорь встречается {n} раз (ожидался 1):\n---\n{old}\n---")
                print("    Ничего не записано. Пришли файл — обновлю якоря.")
                return 1
            text = text.replace(old, new)
        staged[path] = text

    # ── ai/claude.py: замена SYSTEM_PROMPT целиком по границам ──
    path = root / "ai/claude.py"
    text = path.read_text(encoding="utf-8")
    start_anchor = 'SYSTEM_PROMPT = """'
    end_anchor = 'Не вываливай список возможностей — скучно."""'
    if text.count(start_anchor) != 1 or text.count(end_anchor) != 1:
        print("[!] ai/claude.py: не нашёл границы SYSTEM_PROMPT — пришли файл.")
        return 1
    i = text.index(start_anchor)
    j = text.index(end_anchor) + len(end_anchor)
    text = text[:i] + 'SYSTEM_PROMPT = """' + NEW_PROMPT + '"""' + text[j:]
    staged[path] = text

    for path, text in staged.items():
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path.write_text(text, encoding="utf-8")
        print(f"[ok] {path.relative_to(root)}  (бэкап: {backup.name})")

    print("\nГотово. Дальше: env CLAUDE_MODEL=claude-sonnet-4-6 на Railway,")
    print("редеплой бота. БД уже должна быть мигрирована served_count.sql.")
    return 0


if __name__ == "__main__":
    sys.exit(main())