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


def tg_send(text: str):
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


PRIORITY_LABEL = {5: "🔴", 4: "🟠", 3: "🟡", 2: "🔵", 1: "⚪"}
STATUS_ICON    = {"pending": "○", "in_progress": "▶", "done": "✓", "cancelled": "✗"}


def fmt_todo(t: dict, show_time: bool = False, show_quadrant: bool = True) -> str:
    icon = PRIORITY_LABEL.get(t.get("priority", 3), "🟡")
    status = STATUS_ICON.get(t.get("status", "pending"), "○")
    title = t["title"][:60]
    time_str = ""
    if show_time and t.get("due_at") and t.get("has_time"):
        try:
            dt = datetime.fromisoformat(t["due_at"])
            time_str = f" <code>{dt.strftime('%H:%M')}</code>"
        except ValueError:
            pass
    quad = ""
    if show_quadrant:
        q = t.get("quadrant")
        quad = f" {q['icon']}" if q else " ❔"
    return f"{status} {icon} {title}{quad}{time_str}"


def main():
    if not all([MEMORY_API_KEY, TELEGRAM_TOKEN, ALLOWED_USER_ID]):
        print("Missing env vars: MEMORY_API_KEY, TELEGRAM_TOKEN, ALLOWED_USER_ID")
        return

    data = api_get("/todos/today")
    date = data.get("date", "?")
    scheduled    = data.get("scheduled", [])
    today_no_time= data.get("today_no_time", [])
    overdue      = data.get("overdue", [])
    anytime      = data.get("anytime", [])

    total = len(scheduled) + len(today_no_time) + len(overdue)

    if total == 0 and not anytime:
        tg_send(f"☀️ <b>Доброе утро!</b> На сегодня ({date}) задач нет.")
        return

    lines = [f"☀️ <b>Доброе утро! План на {date}</b>\n"]
    shown_ids: set = set()   # чтобы понедельничный блок не дублировал вывод выше

    if overdue:
        lines.append(f"⚠️ <b>Просрочено ({len(overdue)}):</b>")
        for t in overdue[:5]:
            due = t.get("due_at", "")[:10]
            lines.append(f"  {fmt_todo(t)} <i>({due})</i>")
            shown_ids.add(t["id"])
        if len(overdue) > 5:
            lines.append(f"  ...ещё {len(overdue)-5}")
        lines.append("")

    if scheduled:
        lines.append(f"🕐 <b>По времени ({len(scheduled)}):</b>")
        for t in scheduled:
            lines.append(f"  {fmt_todo(t, show_time=True)}")
            shown_ids.add(t["id"])
        lines.append("")

    if today_no_time:
        lines.append(f"📋 <b>На сегодня без времени ({len(today_no_time)}):</b>")
        for t in today_no_time:
            lines.append(f"  {fmt_todo(t)}")
            shown_ids.add(t["id"])
        lines.append("")

    if anytime:
        lines.append(f"📌 <b>Без даты ({len(anytime)}):</b>")
        for t in anytime[:8]:
            lines.append(f"  {fmt_todo(t)}")
            shown_ids.add(t["id"])
        if len(anytime) > 8:
            lines.append(f"  ...ещё {len(anytime)-8}")

    # По понедельникам — напоминание про важное-несрочное. Это тот квадрант,
    # который тихо проваливается: не горит, поэтому не делается никогда.
    if datetime.now().weekday() == 0:
        try:
            matrix = api_get("/todos/matrix")
            plan = [t for t in (matrix["quadrants"].get("важное-несрочное") or [])
                    if t["id"] not in shown_ids]
            unclassified = matrix["quadrants"].get("неклассифицировано") or []
            if plan:
                lines.append("")
                lines.append(f"🎯 <b>Важное, но несрочное ({len(plan)})</b> — то, что стоит планировать:")
                for t in plan[:6]:
                    lines.append(f"  {fmt_todo(t, show_quadrant=False)}")
                if len(plan) > 6:
                    lines.append(f"  ...ещё {len(plan)-6}")
            if unclassified:
                lines.append("")
                lines.append(f"❔ <b>Не разложено по матрице: {len(unclassified)}</b> — скажи боту, что важно и что срочно")
        except Exception as e:
            print(f"Matrix block skipped: {e}")

    tg_send("\n".join(lines))
    print(f"Morning digest sent: {total} tasks for today, {len(anytime)} anytime.")


if __name__ == "__main__":
    main()
