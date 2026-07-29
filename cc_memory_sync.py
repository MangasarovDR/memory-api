#!/usr/bin/env python3
"""
PostToolUse hook для Claude Code.
Когда Claude Code пишет файл в memory/, синхронизирует его с Memory API.
Вызов: python3 cc_memory_sync.py < stdin (JSON от Claude Code)
"""

import sys
import json
import os
import re
import urllib.request
import urllib.parse

MEMORY_API_URL = os.getenv("MEMORY_API_URL", "https://memory.automatiko.ru")

def _load_api_key() -> str:
    key = os.getenv("MEMORY_API_KEY", "")
    if key:
        return key
    # Fallback: читаем напрямую из файла конфига
    env_file = "/opt/memory-api/.env"
    try:
        with open(env_file) as f:
            for line in f:
                if line.startswith("MEMORY_API_KEY="):
                    return line.strip().split("=", 1)[1]
    except Exception:
        pass
    return ""

MEMORY_API_KEY = _load_api_key()
MEMORY_DIR = os.path.expanduser("~/.claude/projects/-root/memory/")


def _post(path: str, body: dict):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        MEMORY_API_URL + path,
        data=data,
        headers={
            "Authorization": f"Bearer {MEMORY_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\n(.*?)\n---\n?", text, re.DOTALL)
    if not m:
        return {}, text
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm, text[m.end():]


def _slug_from_path(path: str) -> str:
    name = os.path.splitext(os.path.basename(path))[0]
    return re.sub(r'[^\w\-]', '-', name).strip('-').lower()


def _type_from_fm(fm: dict) -> str:
    t = fm.get("type", "").lower()
    if t in ("user", "feedback", "project", "reference"):
        return t
    return "project"


def sync_file(file_path: str):
    if not os.path.isfile(file_path):
        return
    if not file_path.endswith(".md"):
        return
    # Игнорируем MEMORY.md (индекс)
    if os.path.basename(file_path) == "MEMORY.md":
        return

    try:
        content = open(file_path, encoding="utf-8").read()
        fm, body = _parse_frontmatter(content)
        slug = fm.get("name") or _slug_from_path(file_path)
        mem_type = _type_from_fm(fm)
        description = fm.get("description", slug)

        _post("/memories", {
            "slug": slug,
            "type": mem_type,
            "description": description,
            "body_md": body.strip(),
            "tags": [],
        })
    except Exception as e:
        print(f"[memory-sync] {file_path}: {e}", file=sys.stderr)


def main():
    if not MEMORY_API_KEY:
        sys.exit(0)

    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})

    if tool not in ("Write", "Edit"):
        sys.exit(0)

    file_path = tool_input.get("file_path", "")
    if not file_path:
        sys.exit(0)

    # Синхронизируем только файлы из папки memory/
    if MEMORY_DIR.rstrip("/") not in file_path:
        sys.exit(0)

    sync_file(file_path)


if __name__ == "__main__":
    main()
