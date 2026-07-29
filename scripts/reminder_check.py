#!/usr/bin/env python3
"""
Проверяет туду-задачи с наступившим remind_at и шлёт напоминание в Telegram.
После отправки сбрасывает remind_at чтобы не слать повторно.
"""

import os
import json
import urllib.request
from datetime import datetime, timedelta

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


def api_patch(path: str, body: dict):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        MEMORY_API_URL + path,
        data=data,
        headers={"Authorization": f"Bearer {MEMORY_API_KEY}", "Content-Type": "application/json"},
        method="PATCH",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
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


def main():
    if not all([MEMORY_API_KEY, TELEGRAM_TOKEN, ALLOWED_USER_ID]):
        print("Missing env vars")
        return

    todos = api_get("/todos/pending-reminders")
    if not todos:
        return

    for t in todos:
        icon = PRIORITY_LABEL.get(t.get("priority", 3), "🟡")
        title = t["title"]
        desc = t.get("description", "")
        due = t.get("due_at", "")
        due_str = f"\n📅 Срок: <code>{due[:16]}</code>" if due else ""

        text = f"⏰ <b>Напоминание!</b>\n\n{icon} <b>{title}</b>"
        if desc:
            text += f"\n{desc[:200]}"
        text += due_str

        repeat = t.get("repeat_days")
        if repeat:
            text += f"\n\n<i>(повторяю раз в {repeat} дн., пока задача открыта)</i>"

        try:
            tg_send(text)
            if repeat:
                # Периодическое напоминание: сдвигаем вперёд, а не гасим
                nxt = (datetime.now() + timedelta(days=int(repeat))).strftime("%Y-%m-%d %H:%M")
                api_patch(f"/todos/{t['id']}", {"remind_at": nxt})
                print(f"Reminder sent for todo #{t['id']}, next at {nxt}: {title[:40]}")
            else:
                api_patch(f"/todos/{t['id']}", {"remind_at": None})
                print(f"Reminder sent for todo #{t['id']}: {title[:40]}")
        except Exception as e:
            print(f"Failed to send reminder for #{t['id']}: {e}")


if __name__ == "__main__":
    main()
