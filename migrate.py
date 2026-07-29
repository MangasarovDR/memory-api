#!/usr/bin/env python3
"""
Migrates memory .md files → Memory API.
Run: python3 /opt/memory-api/migrate.py
"""

import json
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path

MEMORY_DIR = Path("/root/.claude/projects/-root/memory")
API_BASE   = "http://127.0.0.1:8765"
API_KEY    = "deRJE5RaAPHHIuzwGf9c_BQE0aOzr908rCgpoZXOj5s"

# Explicit tag mapping per file (slug → tags)
TAGS: dict[str, list[str]] = {
    "feedback-claude-bot-obsidian-docs":          ["prefs", "tech"],
    "feedback-document-style":                    ["prefs"],
    "feedback-obsidian-log-all-messages":         ["prefs", "tech"],
    "feedback-project-lifecycle":                 ["prefs", "work"],
    "feedback-respond-in-russian":                ["prefs"],
    "feedback-subagent-batching":                 ["prefs", "tech"],
    "feedback-subagent-model-selection":          ["prefs", "tech"],
    "project-automatiko-product":                 ["automatiko", "work"],
    "project-automatiko-vision":                  ["automatiko", "work"],
    "claude_bot_idea_validator":                  ["tech"],
    "claude_bot_oneprovider_balance_endpoint":    ["tech"],
    "claude_bot_weekly_audit":                    ["tech"],
    "oneprovider-api-quirks":                     ["tech"],
    "project-perplexity-archive-curation":        ["tech", "work"],
    "user-medical-history":                       ["profile", "health"],
    "user-profile":                               ["profile"],
}


def parse_file(path: Path) -> dict | None:
    text = path.read_text(encoding="utf-8")

    # Split on frontmatter delimiters
    parts = re.split(r"^---\s*$", text, maxsplit=2, flags=re.MULTILINE)
    if len(parts) < 3:
        print(f"  SKIP (no frontmatter): {path.name}")
        return None

    front, body = parts[1], parts[2].strip()

    # Parse frontmatter fields we need
    def get(key: str) -> str:
        m = re.search(rf'^{key}:\s*"?([^"\n]+)"?\s*$', front, re.MULTILINE)
        return m.group(1).strip() if m else ""

    slug = get("name")
    description = get("description")
    # type is nested under metadata:, but also appears as bare key
    type_m = re.search(r'^\s+type:\s*(\w+)', front, re.MULTILINE)
    mem_type = type_m.group(1).strip() if type_m else ""

    if not slug or not mem_type:
        print(f"  SKIP (missing name/type): {path.name}")
        return None

    return {
        "slug":        slug,
        "type":        mem_type,
        "description": description,
        "tags":        TAGS.get(slug, []),
        "body_md":     body,
    }


def post(payload: dict) -> bool:
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        f"{API_BASE}/memories",
        data=data,
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            print(f"  OK  [{result['type']}] {result['slug']}  tags={result['tags']}")
            return True
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"  ERR {payload['slug']}: HTTP {e.code} — {body}")
        return False


def main():
    files = sorted(p for p in MEMORY_DIR.glob("*.md") if p.name != "MEMORY.md")
    print(f"Найдено файлов: {len(files)}\n")

    ok = err = skip = 0
    for path in files:
        payload = parse_file(path)
        if payload is None:
            skip += 1
            continue
        if post(payload):
            ok += 1
        else:
            err += 1

    print(f"\nГотово: {ok} загружено, {err} ошибок, {skip} пропущено")
    if err:
        sys.exit(1)


if __name__ == "__main__":
    main()
