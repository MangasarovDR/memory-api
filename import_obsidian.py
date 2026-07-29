#!/usr/bin/env python3
"""
Чертоги Разума — одноразовый импорт Obsidian → Memory API.
Запуск: python3 /opt/memory-api/import_obsidian.py
"""

import os
import re
import sys
import json
import urllib.request
import urllib.parse
import urllib.error

OBSIDIAN_DIR = "/opt/claude-bot/obsidian"
API_URL      = os.getenv("MEMORY_API_URL", "https://memory.automatiko.ru")
API_KEY      = os.getenv("MEMORY_API_KEY", "")

# Папка → (тип, важность)
FOLDER_MAP = {
    "Давид":    ("user",      4),
    "Знания":   ("knowledge", 3),
    "Идеи":     ("idea",      2),
    "Проекты":  ("project",   4),
}
RULES_FILE = "_ПРАВИЛА.md"

SKIP_DIRS = {".obsidian", ".vault-operator", ".stfolder"}

# ─── helpers ──────────────────────────────────────────────────────────────────

def strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            return text[end + 3:].lstrip("\n")
    return text

def first_meaningful_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if line and len(line) > 5:
            return line[:150]
    return ""

def to_slug(folder: str, filename: str) -> str:
    """Generates a stable kebab-case slug from folder + filename."""
    prefix = {
        "Давид":   "user",
        "Знания":  "knowledge",
        "Идеи":    "idea",
        "Проекты": "project",
    }.get(folder, folder.lower())
    name = os.path.splitext(filename)[0]
    name = re.sub(r"[^\w\s-]", "", name).strip().replace("_", "-").replace(" ", "-").lower()
    name = re.sub(r"-+", "-", name)
    return f"{prefix}-{name}"[:80]

def api_post(path: str, body: dict) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(
        API_URL + path, data=data,
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

def api_get(path: str) -> list:
    req = urllib.request.Request(
        API_URL + path,
        headers={"Authorization": f"Bearer {API_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

# ─── collect files ────────────────────────────────────────────────────────────

def collect() -> list[dict]:
    items = []

    for entry in os.scandir(OBSIDIAN_DIR):
        if entry.name in SKIP_DIRS or entry.name.startswith("."):
            continue

        # Корневые файлы (например _ПРАВИЛА.md)
        if entry.is_file() and entry.name.endswith(".md"):
            if entry.name == RULES_FILE:
                text = open(entry.path, encoding="utf-8").read()
                body = strip_frontmatter(text)
                desc = first_meaningful_line(body) or "Правила работы Claude"
                items.append({
                    "slug":        "feedback-pravila",
                    "type":        "feedback",
                    "description": desc,
                    "body_md":     text,
                    "importance":  5,
                    "source":      "obsidian",
                    "tags":        ["rules", "claude"],
                })
            continue

        if not entry.is_dir():
            continue

        folder_name = entry.name
        if folder_name not in FOLDER_MAP:
            continue

        mem_type, importance = FOLDER_MAP[folder_name]

        # Рекурсивно собираем файлы из папки (включая подпапки)
        for root, dirs, files in os.walk(entry.path):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fname in sorted(files):
                if not fname.endswith(".md"):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    text = open(fpath, encoding="utf-8").read()
                except Exception as e:
                    print(f"  ⚠ Не удалось прочитать {fpath}: {e}")
                    continue

                body = strip_frontmatter(text)
                desc = first_meaningful_line(body) or os.path.splitext(fname)[0].replace("_", " ")

                # Подпапки дают дополнительный тег (например Здоровье → health)
                rel_folder = os.path.relpath(root, entry.path)
                subfolder_tag = []
                if rel_folder != ".":
                    subfolder_tag = [rel_folder.lower().replace(" ", "_")]

                # slug: user-zdorove-analizy-2026
                if rel_folder != ".":
                    sub_slug = re.sub(r"[^\w\s-]", "", rel_folder).strip().lower()
                    sub_slug = re.sub(r"[\s_]+", "-", sub_slug)
                    slug = to_slug(folder_name, f"{sub_slug}-{fname}")
                else:
                    slug = to_slug(folder_name, fname)

                items.append({
                    "slug":        slug,
                    "type":        mem_type,
                    "description": desc[:150],
                    "body_md":     text,
                    "importance":  importance,
                    "source":      "obsidian",
                    "tags":        [folder_name.lower()] + subfolder_tag,
                })

    return items

# ─── import ───────────────────────────────────────────────────────────────────

def main():
    if not API_KEY:
        print("ERROR: MEMORY_API_KEY not set")
        sys.exit(1)

    print(f"Obsidian dir: {OBSIDIAN_DIR}")
    print(f"API:          {API_URL}")
    print()

    items = collect()
    print(f"Найдено файлов: {len(items)}\n")

    # Получаем существующие slugs чтобы не дублировать (только для info)
    try:
        existing = {m["slug"] for m in api_get("/memories?limit=1000")}
        print(f"Уже в базе: {len(existing)} записей")
    except Exception as e:
        print(f"Не удалось получить список: {e}")
        existing = set()

    ok = skip = fail = 0
    for item in items:
        action = "обновлён" if item["slug"] in existing else "создан"
        try:
            api_post("/memories", item)
            print(f"  ✅ [{item['type']}] {item['slug'][:60]} — {action}")
            ok += 1
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            print(f"  ❌ {item['slug']}: HTTP {e.code} — {body[:120]}")
            fail += 1
        except Exception as e:
            print(f"  ❌ {item['slug']}: {e}")
            fail += 1

    print(f"\n{'─'*50}")
    print(f"Готово: {ok} успешно, {fail} ошибок")
    print(f"Итого в базе: ~{len(existing) + ok} записей")

if __name__ == "__main__":
    main()
