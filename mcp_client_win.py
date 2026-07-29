#!/usr/bin/env python3
"""MCP stdio-клиент для Memory API. Запускается Claude Desktop на Windows."""

import sys
import json
import os
import urllib.request
import urllib.parse
import urllib.error

MEMORY_API_URL = os.getenv("MEMORY_API_URL", "https://memory.automatiko.ru")
MEMORY_API_KEY = os.getenv("MEMORY_API_KEY", "")


def api(method, path, params=None, body=None):
    url = MEMORY_API_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Authorization": f"Bearer {MEMORY_API_KEY}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()}")


TOOLS = [
    {
        "name": "memory_context",
        "description": "Получить релевантные воспоминания по теме. Вызывай перед каждым ответом.",
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string", "description": "Тема или запрос"}},
            "required": ["message"],
        },
    },
    {
        "name": "memory_search",
        "description": "Полнотекстовый поиск по всем воспоминаниям.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Поисковый запрос"},
                "limit": {"type": "integer", "description": "Макс. результатов", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_save",
        "description": "Сохранить воспоминание. type: user | feedback | project | reference | knowledge | idea.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string"},
                "type": {"type": "string", "enum": ["user", "feedback", "project", "reference", "knowledge", "idea"]},
                "description": {"type": "string"},
                "body": {"type": "string"},
                "importance": {"type": "integer", "description": "1–5, по умолч. 3", "default": 3},
                "tags": {"type": "string", "description": "Теги через запятую"},
            },
            "required": ["slug", "type", "description", "body"],
        },
    },
    {
        "name": "memory_list",
        "description": "Список воспоминаний. Фильтры: type и/или tags.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "description": "Фильтр по типу"},
                "tags": {"type": "string", "description": "Фильтр по тегам"},
            },
        },
    },
]


def call_tool(name, args):
    try:
        if name == "memory_context":
            data = api("GET", "/memories/context", params={"message": args["message"]})
            mems = data.get("memories", [])
            if not mems:
                return "Релевантных воспоминаний не найдено."
            return "\n\n".join(f"[{m['slug']}] {m['description']}\n{m['body_md'][:600]}" for m in mems)

        elif name == "memory_search":
            results = api("GET", "/memories/search", params={"q": args["query"], "limit": args.get("limit", 5)})
            if not results:
                return "Ничего не найдено."
            return "\n\n---\n\n".join(f"### {r['slug']}\n{r['description']}\n\n{r['body_md'][:500]}" for r in results)

        elif name == "memory_save":
            tags = [t.strip() for t in args.get("tags", "").split(",") if t.strip()]
            api("POST", "/memories", body={
                "slug": args["slug"], "type": args["type"],
                "description": args["description"], "body_md": args["body"],
                "importance": args.get("importance", 3),
                "source": "claude-desktop",
                "tags": tags,
            })
            return f"✅ Сохранено: {args['slug']}"

        elif name == "memory_list":
            params = {"limit": 100}
            if args.get("type"): params["type"] = args["type"]
            if args.get("tags"): params["tags"] = args["tags"]
            results = api("GET", "/memories", params=params)
            if not results:
                return "Воспоминаний нет."
            lines = [f"- [{r['type']}] **{r['slug']}** — {r['description']}" for r in results]
            return f"Всего: {len(results)}\n\n" + "\n".join(lines)

        else:
            return f"Неизвестный инструмент: {name}"
    except Exception as e:
        return f"Ошибка: {e}"


def send(obj):
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    sys.stdout.write(line)
    sys.stdout.flush()


def main():
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue

        method = msg.get("method", "")
        req_id = msg.get("id")

        if method == "initialize":
            send({"jsonrpc": "2.0", "id": req_id, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "memory-api", "version": "1.0"},
            }})

        elif method == "notifications/initialized":
            pass

        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})

        elif method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name", "")
            args = params.get("arguments", {})
            result = call_tool(name, args)
            send({"jsonrpc": "2.0", "id": req_id, "result": {
                "content": [{"type": "text", "text": result}],
                "isError": False,
            }})

        elif req_id is not None:
            send({"jsonrpc": "2.0", "id": req_id, "error": {
                "code": -32601, "message": f"Method not found: {method}",
            }})


if __name__ == "__main__":
    main()
