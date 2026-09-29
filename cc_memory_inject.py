#!/usr/bin/env python3
"""
UserPromptSubmit-хук: подаёт в контекст компактный индекс Чертогов Разума.

Зачем. В CLAUDE.md записано «importance >= 4 — всегда в контексте», но подавать
это было некому: хука на чтение не существовало, а /memories/context отдаёт
~115 000 токенов (97 записей с телами) — столько в каждый ход не влезет.
Из-за этого правила, не совпадающие по словам с темой задачи, не находились
семантическим поиском и молча не применялись.

Что делает:
  * первое сообщение сессии — одной строкой на запись весь слой правил
    (feedback + user): «как работать с Давидом» и «кто он такой»;
  * каждое сообщение — до 3 релевантных записей, которых в этой сессии ещё
    не показывали;
  * тела не подаёт: их читают по slug через GET /memories/<slug>.

Никогда не роняет ход: любая ошибка — тихий exit 0 без вывода.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

API = os.getenv("MEMORY_API_URL", "https://memory.automatiko.ru")
ENV_FILE = "/opt/memory-api/.env"
STATE_DIR = "/opt/memory-api/state/inject"
TIMEOUT = 4
DESC_MAX = 130          # обрезка описания, чтобы индекс не разрастался
TOP_RELEVANT = 3
STATE_TTL = 7 * 24 * 3600


def api_key():
    key = os.getenv("MEMORY_API_KEY", "")
    if key:
        return key
    with open(ENV_FILE, encoding="utf-8") as f:
        for line in f:
            if line.startswith("MEMORY_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("нет MEMORY_API_KEY")


def get(path, key):
    req = urllib.request.Request(API + path, headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = json.load(r)
    if isinstance(data, dict):
        return data.get("memories", [])
    return data


def line(m):
    d = (m.get("description") or "").strip().replace("\n", " ")
    if len(d) > DESC_MAX:
        d = d[:DESC_MAX - 1].rstrip() + "…"
    return f"- {m['slug']} — {d}"


def state_path(sid):
    safe = "".join(c for c in (sid or "nosession") if c.isalnum() or c in "-_")[:64]
    return os.path.join(STATE_DIR, safe + ".json")


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"shown": [], "index_done": False}


def save_state(path, st):
    os.makedirs(STATE_DIR, exist_ok=True)
    st["ts"] = time.time()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    # уборка старых сессий, чтобы каталог не рос бесконечно
    now = time.time()
    for name in os.listdir(STATE_DIR):
        p = os.path.join(STATE_DIR, name)
        try:
            if now - os.path.getmtime(p) > STATE_TTL:
                os.remove(p)
        except OSError:
            pass


def main():
    hook = json.loads(sys.stdin.read() or "{}")
    prompt = (hook.get("prompt") or "").strip()
    sid = hook.get("session_id") or ""
    sp = state_path(sid)
    st = load_state(sp)
    shown = set(st.get("shown", []))
    key = api_key()
    blocks = []

    if not st.get("index_done"):
        rules = get("/memories?type=feedback&limit=200", key)
        about = get("/memories?type=user&limit=200", key)
        # Свежая передача — первое, что нужно сессии после clear: тему её слов
        # семантический поиск не угадает, поэтому подаём явно и всегда.
        try:
            proj = get("/memories?type=project&limit=200", key)
            hand = [m for m in proj if m["slug"].startswith("peredacha")]
            hand.sort(key=lambda m: m.get("updated_at") or m.get("created_at") or "", reverse=True)
            if hand:
                blocks.append("ПОСЛЕДНЯЯ ПЕРЕДАЧА (читать целиком по slug, если сессия новая):\n"
                              + line(hand[0]))
                shown.add(hand[0]["slug"])
        except Exception:
            pass
        if rules or about:
            head = ("ПАМЯТЬ (Чертоги Разума). Ниже — только slug и описание. "
                    "Тело записи читать по slug: GET /memories/<slug>. "
                    "ЭТИ ПРАВИЛА ПРОВЕРЯТЬ НЕ ТОЛЬКО В НАЧАЛЕ ЗАДАЧИ, НО И В МОМЕНТ "
                    "ОТДАЧИ РАБОТЫ — часть из них про доставку, а не про содержание.")
            blocks.append(head)
            if rules:
                blocks.append("КАК РАБОТАТЬ С ДАВИДОМ (feedback):\n" +
                              "\n".join(line(m) for m in rules))
            if about:
                blocks.append("КТО ТАКОЙ ДАВИД (user):\n" +
                              "\n".join(line(m) for m in about))
            shown.update(m["slug"] for m in rules + about)
            st["index_done"] = True

    if prompt:
        q = urllib.parse.quote(prompt[:300])
        hits = get(f"/memories/search?q={q}&mode=hybrid&limit=8", key)
        new = [m for m in hits if m["slug"] not in shown][:TOP_RELEVANT]
        if new:
            blocks.append("ПО ТЕМЕ ЭТОГО СООБЩЕНИЯ:\n" + "\n".join(line(m) for m in new))
            shown.update(m["slug"] for m in new)

    if not blocks:
        return

    st["shown"] = sorted(shown)
    save_state(sp, st)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n\n".join(blocks),
        },
        "suppressOutput": True,
    }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass        # молчим: подача памяти не должна мешать работе
    sys.exit(0)
