#!/usr/bin/env python3
"""Еженедельное прореживание always-load: importance >= 4 не должен расти без края.

ЗАЧЕМ. Ярусов в /memories/context три, но первый — «всё, что importance >= 4» —
брался целиком. К 22.08.2026 таких записей стало 221 при потолке ответа 20:
семантический поиск не получал НИ ОДНОГО места, а две трети «всегда в контексте»
молча не доезжали. Потолок подняли до 100, но 221 в 100 всё равно не влезает —
значит, в ярус должно попадать только то, что нужно ДЕЙСТВИТЕЛЬНО всегда.

ПРАВИЛО (по типам, а не по темам — тему решает поиск):
  user, feedback  — не трогаем никогда. Кто такой Давид и как с ним работать
                    нужно в каждом разговоре независимо от темы.
  project         — важен, пока идёт. Не правился дольше PROJECT_DAYS → 3.
  knowledge,      — грабли, формулы, справочники. Всплывают ПО ТЕМЕ, для этого
  reference, idea   и существует поиск. Старше OTHER_DAYS → 3.

Ниже 3 не опускаем и ничего не удаляем: запись уходит из «всегда», но остаётся
полноценно доступной поиском.

ГРАБЛЯ, РАДИ КОТОРОЙ ЗДЕСЬ ПОЛОВИНА КОДА. На memories висит триггер
`updated_at_trigger`, который на ЛЮБОЙ UPDATE ставит updated_at = сейчас.
Прямое понижение важности пометило бы все 130 записей как «только что
изменённые» — и правило возраста, на котором стоит этот же скрипт, перестало бы
работать со следующей недели. Поэтому триггер на время пакета снимается и
возвращается обратно, а updated_at выставляется руками в прежнее значение.
"""
import argparse
import datetime as dt
import json
import os
import sqlite3
import urllib.request
from pathlib import Path

DB = Path(os.getenv("MEMORY_DB", "/opt/memory-api/data/memory.db"))
LOG = DB.parent / "prune.log"
KEEP_TYPES = ("user", "feedback")
PROJECT_DAYS = 7      # проект без правки неделю — уже не «то, чем занят сейчас»
OTHER_DAYS = 3        # знание живёт поиском, а не постоянным присутствием
FLOOR = 3             # ниже не опускаем никогда

TRIGGER_SQL = """CREATE TRIGGER updated_at_trigger AFTER UPDATE ON memories BEGIN
            UPDATE memories SET updated_at = CURRENT_TIMESTAMP WHERE id = new.id;
        END"""


def age_days(ts: str, today: dt.date) -> int:
    return (today - dt.date.fromisoformat(ts[:10])).days


def verdict(row, today) -> int | None:
    """Новая важность или None, если запись остаётся как есть."""
    if row["type"] in KEEP_TYPES or row["importance"] < 4:
        return None
    limit = PROJECT_DAYS if row["type"] == "project" else OTHER_DAYS
    if age_days(row["updated_at"], today) <= limit:
        return None
    return FLOOR


def tg_send(text: str) -> None:
    """Отчёт в телеграм. Токен лежит у бота — своего у memory-api нет."""
    env = {}
    try:
        for line in open("/opt/claude-bot/.env", encoding="utf-8"):
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except OSError:
        return
    tok, chat = env.get("TELEGRAM_TOKEN"), env.get("ALLOWED_USER_ID")
    if not tok or not chat:
        return
    try:
        rq = urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            data=json.dumps({"chat_id": chat, "text": text}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(rq, timeout=15).read()
    except Exception:
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="записать изменения; без флага — только показать")
    ap.add_argument("--quiet", action="store_true", help="без телеграма")
    a = ap.parse_args()

    today = dt.date.today()
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT id, slug, type, importance, updated_at FROM memories "
                      "WHERE importance >= 4").fetchall()
    plan = [(r, v) for r in rows if (v := verdict(r, today)) is not None]
    before = len(rows)
    after = before - len(plan)

    core = sum(1 for r in rows if r["type"] in KEEP_TYPES)
    head = (f"Прореживание памяти {today:%d.%m.%Y}\n"
            f"always-load: {before} → {after} (понижено {len(plan)})\n"
            f"из них несменяемое ядро user+feedback: {core}")

    if not a.apply:
        print(head + "\n(пробный прогон, ничего не записано)\n")
        for r, v in sorted(plan, key=lambda x: -age_days(x[0]["updated_at"], today))[:25]:
            print(f"  {age_days(r['updated_at'], today):>4}д  "
                  f"imp{r['importance']}→{v}  {r['type']:<10} {r['slug']}")
        if len(plan) > 25:
            print(f"  … и ещё {len(plan) - 25}")
        return

    # откат: точный снимок того, что меняем
    roll = DB.parent / f"prune-rollback-{today:%Y%m%d}.json"
    roll.write_text(json.dumps(
        [{"id": r["id"], "slug": r["slug"], "importance": r["importance"],
          "updated_at": r["updated_at"]} for r, _ in plan],
        ensure_ascii=False, indent=1), encoding="utf-8")

    # триггер снимаем, чтобы возраст записей не обнулился (см. шапку файла)
    with db:
        db.execute("DROP TRIGGER IF EXISTS updated_at_trigger")
        for r, v in plan:
            db.execute("UPDATE memories SET importance = ?, updated_at = ? WHERE id = ?",
                       (v, r["updated_at"], r["id"]))
        db.execute(TRIGGER_SQL)

    # проверка, что триггер вернулся: без него правки перестанут датироваться
    ok = db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' "
                    "AND name='updated_at_trigger'").fetchone()
    tail = "" if ok else "\n⚠ ТРИГГЕР updated_at НЕ ВОССТАНОВЛЕН — чинить руками"
    left = db.execute("SELECT COUNT(*) c FROM memories WHERE importance >= 4").fetchone()["c"]
    msg = f"{head}\nсейчас в ярусе: {left}, откат: {roll.name}{tail}"
    print(msg)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M}  {before}→{left}  "
                f"понижено {len(plan)}  откат {roll.name}\n")
    if not a.quiet:
        tg_send(msg)


if __name__ == "__main__":
    main()
