#!/usr/bin/env python3
"""
Утренний дайджест туду-листа. Запускается через cron, шлёт в Telegram.
"""

import os
import json
import urllib.request
from datetime import datetime

MEMORY_API_URL = os.getenv("MEMORY_API_URL", "http://127.0.0.1:8765")
MEMORY_API_KEY = os.getenv("MEMORY_API_KEY", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID", "")


def api_get(path: str):
    req = urllib.request.Request(
        MEMORY_API_URL + path,
        headers={"Authorization": f"Bearer {MEMORY_API_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


TG_LIMIT = 4096


def _tg_send_one(text: str):
    data = json.dumps({
        "chat_id": ALLOWED_USER_ID,
        "text": text,
        "parse_mode": "HTML",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def tg_send(text: str):
    """Дайджест не обрезаем — если не влезает в лимит Telegram, режем на части по строкам."""
    if len(text) <= TG_LIMIT:
        return _tg_send_one(text)

    chunks, cur = [], ""
    for line in text.split("\n"):
        # Одна строка длиннее лимита — разрезаем жёстко, иначе она не отправится вообще
        while len(line) > TG_LIMIT:
            if cur:
                chunks.append(cur); cur = ""
            chunks.append(line[:TG_LIMIT]); line = line[TG_LIMIT:]
        if len(cur) + len(line) + 1 > TG_LIMIT:
            chunks.append(cur); cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)

    res = None
    for n, chunk in enumerate(chunks, 1):
        suffix = f"\n\n<i>(часть {n} из {len(chunks)})</i>" if len(chunks) > 1 else ""
        res = _tg_send_one(chunk + suffix)
    return res


PRIORITY_LABEL = {5: "🔴", 4: "🟠", 3: "🟡", 2: "🔵", 1: "⚪"}
STATUS_ICON    = {"pending": "○", "in_progress": "▶", "done": "✓", "cancelled": "✗"}

# Порядок квадрантов при сортировке — вторичный ключ после приоритета.
QUADRANT_ORDER = {
    "важное-срочное":     0,
    "важное-несрочное":   1,
    "неважное-срочное":   2,
    "неважное-несрочное": 3,
}


def sort_todos(todos: list) -> list:
    """Сначала приоритет (5 → 1), внутри приоритета — по матрице Эйзенхауэра."""
    def key(t):
        q = t.get("quadrant") or {}
        return (
            -int(t.get("priority") or 3),
            QUADRANT_ORDER.get(q.get("key"), 4),
            t.get("due_at") or "9999",
            t.get("id") or 0,
        )
    return sorted(todos, key=key)


_MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн",
           "июл", "авг", "сен", "окт", "ноя", "дек"]


def human_due(t: dict) -> str:
    """ISO-дата → человеческая: «сегодня 12:00», «вчера», «3 авг»."""
    raw = t.get("due_at")
    if not raw:
        return ""
    try:
        d = datetime.fromisoformat(raw.replace("T", " "))
    except ValueError:
        return raw[:16]
    delta = (d.date() - datetime.now().date()).days
    if delta == 0:
        day = "сегодня"
    elif delta == 1:
        day = "завтра"
    elif delta == -1:
        day = "вчера"
    elif d.year == datetime.now().year:
        day = f"{d.day} {_MONTHS[d.month - 1]}"
    else:
        day = f"{d.day} {_MONTHS[d.month - 1]} {d.year}"
    return f"{day} {d:%H:%M}" if t.get("has_time") else day


def fmt_todo(t: dict, show_due: bool = False, show_quadrant: bool = True) -> str:
    """Одна задача — одна строка: статус, приоритет, квадрант · название · срок · теги · id."""
    icon = PRIORITY_LABEL.get(t.get("priority", 3), "🟡")
    status = STATUS_ICON.get(t.get("status", "pending"), "○")
    quad = ""
    if show_quadrant:
        q = t.get("quadrant")
        quad = q["icon"] if q else "❔"

    parts = [f"{status} {icon}{quad} {t['title']}"]
    if show_due:
        due = human_due(t)
        if due:
            parts.append(f"<b>{due}</b>")
    tags = t.get("tags") or []
    if tags:
        parts.append(" ".join(f"#{x}" for x in tags))
    parts.append(f"id {t['id']}")
    return " · ".join(parts)


# --- Временная шпаргалка (удалить этот блок и его вставки, когда запомнится) ---
CHEATSHEET = (
    "\n\n──────────\n"
    "🖥️ <b>tmux — шпаргалка</b>\n"
    "Старт новой:\n<code>tmux new -s claude</code>\n"
    "Продолжить (с любого устройства):\n<code>tmux attach -d -t claude</code>"
)


def main():
    if not all([MEMORY_API_KEY, TELEGRAM_TOKEN, ALLOWED_USER_ID]):
        print("Missing env vars: MEMORY_API_KEY, TELEGRAM_TOKEN, ALLOWED_USER_ID")
        return

    data = api_get("/todos/today")
    date = data.get("date", "?")
    # «По времени» остаётся хронологическим — это расписание дня.
    # Остальные блоки — по приоритету, внутри приоритета по матрице.
    scheduled    = data.get("scheduled", [])
    today_no_time= sort_todos(data.get("today_no_time", []))
    overdue      = sort_todos(data.get("overdue", []))
    anytime      = sort_todos(data.get("anytime", []))

    total = len(scheduled) + len(today_no_time) + len(overdue)

    if total == 0 and not anytime:
        tg_send(f"☀️ <b>Доброе утро!</b> На сегодня ({date}) задач нет." + CHEATSHEET)
        return

    lines = [f"☀️ <b>Доброе утро! План на {date}</b>\n"]
    shown_ids: set = set()   # чтобы понедельничный блок не дублировал вывод выше

    if overdue:
        lines.append(f"⚠️ <b>Просрочено — {len(overdue)}</b>")
        for t in overdue:
            lines.append(fmt_todo(t, show_due=True))
            shown_ids.add(t["id"])
        lines.append("")

    if scheduled:
        lines.append(f"🕐 <b>По времени — {len(scheduled)}</b>")
        for t in scheduled:
            lines.append(fmt_todo(t, show_due=True))
            shown_ids.add(t["id"])
        lines.append("")

    if today_no_time:
        lines.append(f"📋 <b>На сегодня без времени — {len(today_no_time)}</b>")
        for t in today_no_time:
            lines.append(fmt_todo(t))
            shown_ids.add(t["id"])
        lines.append("")

    if anytime:
        lines.append(f"📌 <b>Без даты — {len(anytime)}</b>")
        for t in anytime:
            lines.append(fmt_todo(t))
            shown_ids.add(t["id"])

    # По понедельникам — напоминание про важное-несрочное. Это тот квадрант,
    # который тихо проваливается: не горит, поэтому не делается никогда.
    if datetime.now().weekday() == 0:
        try:
            matrix = api_get("/todos/matrix")
            plan = sort_todos([t for t in (matrix["quadrants"].get("важное-несрочное") or [])
                               if t["id"] not in shown_ids])
            unclassified = matrix["quadrants"].get("неклассифицировано") or []
            if plan:
                lines.append("")
                lines.append(f"🎯 <b>Важное, но несрочное — {len(plan)}</b>, то, что стоит планировать")
                for t in plan:
                    lines.append(fmt_todo(t, show_quadrant=False))
            if unclassified:
                lines.append("")
                lines.append(f"❔ <b>Не разложено по матрице — {len(unclassified)}</b>, скажи боту, что важно и что срочно")
        except Exception as e:
            print(f"Matrix block skipped: {e}")

    tg_send("\n".join(lines) + CHEATSHEET)
    print(f"Morning digest sent: {total} tasks for today, {len(anytime)} anytime.")


if __name__ == "__main__":
    main()
