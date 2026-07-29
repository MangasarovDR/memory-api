#!/usr/bin/env python3
"""MCP-сервер для Memory API. Запускается локально, обращается к memory.automatiko.ru."""

import os
import urllib.request
import urllib.parse
import json
from mcp.server.fastmcp import FastMCP

MEMORY_API_URL = os.getenv("MEMORY_API_URL", "https://memory.automatiko.ru")
MEMORY_API_KEY = os.getenv("MEMORY_API_KEY", "")

mcp = FastMCP("memory-api")


def _request(method: str, path: str, params: dict = None, body: dict = None) -> dict:
    url = MEMORY_API_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {MEMORY_API_KEY}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


@mcp.tool()
def memory_context(message: str) -> str:
    """Получить релевантные воспоминания по теме сообщения. Используй перед каждым ответом."""
    if not MEMORY_API_KEY:
        return "MEMORY_API_KEY не задан."
    try:
        data = _request("GET", "/memories/context", params={"message": message})
        memories = data.get("memories", [])
        if not memories:
            return "Релевантных воспоминаний не найдено."
        parts = [f"[{m['slug']}] {m['description']}\n{m['body_md'][:600]}" for m in memories]
        return "\n\n".join(parts)
    except Exception as e:
        return f"Ошибка: {e}"


@mcp.tool()
def memory_search(query: str, limit: int = 5) -> str:
    """Полнотекстовый поиск по всем воспоминаниям."""
    if not MEMORY_API_KEY:
        return "MEMORY_API_KEY не задан."
    try:
        results = _request("GET", "/memories/search", params={"q": query, "limit": limit})
        if not results:
            return "Ничего не найдено."
        parts = [f"### {r['slug']}\n{r['description']}\n\n{r['body_md'][:500]}" for r in results]
        return "\n\n---\n\n".join(parts)
    except Exception as e:
        return f"Ошибка: {e}"


@mcp.tool()
def memory_save(slug: str, type: str, description: str, body: str, tags: str = "") -> str:
    """
    Сохранить или обновить воспоминание.
    type: user | feedback | project | reference
    tags: через запятую, например: 'health,давид'
    """
    if not MEMORY_API_KEY:
        return "MEMORY_API_KEY не задан."
    try:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        _request("POST", "/memories", body={
            "slug": slug,
            "type": type,
            "description": description,
            "body_md": body,
            "tags": tag_list,
        })
        return f"✅ Сохранено: {slug}"
    except Exception as e:
        return f"Ошибка: {e}"


@mcp.tool()
def memory_list(type: str = "", tags: str = "") -> str:
    """Список всех воспоминаний. Можно фильтровать по type и/или tags (через запятую)."""
    if not MEMORY_API_KEY:
        return "MEMORY_API_KEY не задан."
    try:
        params = {"limit": 100}
        if type:
            params["type"] = type
        if tags:
            params["tags"] = tags
        results = _request("GET", "/memories", params=params)
        if not results:
            return "Воспоминаний нет."
        lines = [f"- [{r['type']}] **{r['slug']}** — {r['description']}" for r in results]
        return f"Всего: {len(results)}\n\n" + "\n".join(lines)
    except Exception as e:
        return f"Ошибка: {e}"


if __name__ == "__main__":
    mcp.run()
