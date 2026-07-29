#!/usr/bin/env python3
"""MCP SSE-сервер для Memory API. Слушает на 127.0.0.1:8766, доступен через Caddy."""

import os
import urllib.request
import urllib.parse
import json
import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from mcp.server.fastmcp import FastMCP

MEMORY_API_URL = os.getenv("MEMORY_API_URL", "https://memory.automatiko.ru")


def _load_api_key() -> str:
    key = os.getenv("MEMORY_API_KEY", "")
    if key:
        return key
    try:
        with open("/opt/memory-api/.env") as f:
            for line in f:
                if line.startswith("MEMORY_API_KEY="):
                    return line.strip().split("=", 1)[1]
    except Exception:
        pass
    return ""


MEMORY_API_KEY = _load_api_key()
MCP_TOKEN = MEMORY_API_KEY  # Bearer-токен = тот же ключ

mcp = FastMCP("memory-api")


def _request(method: str, path: str, params: dict = None, body: dict = None) -> dict:
    url = MEMORY_API_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Authorization": f"Bearer {MEMORY_API_KEY}", "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


@mcp.tool()
def memory_context(message: str) -> str:
    """Получить релевантные воспоминания по теме. Вызывай перед каждым ответом."""
    try:
        data = _request("GET", "/memories/context", params={"message": message})
        memories = data.get("memories", [])
        if not memories:
            return "Релевантных воспоминаний не найдено."
        return "\n\n".join(f"[{m['slug']}] {m['description']}\n{m['body_md'][:600]}" for m in memories)
    except Exception as e:
        return f"Ошибка: {e}"


@mcp.tool()
def memory_search(query: str, limit: int = 5) -> str:
    """Полнотекстовый поиск по всем воспоминаниям."""
    try:
        results = _request("GET", "/memories/search", params={"q": query, "limit": limit})
        if not results:
            return "Ничего не найдено."
        return "\n\n---\n\n".join(f"### {r['slug']}\n{r['description']}\n\n{r['body_md'][:500]}" for r in results)
    except Exception as e:
        return f"Ошибка: {e}"


@mcp.tool()
def memory_save(slug: str, type: str, description: str, body: str, tags: str = "") -> str:
    """Сохранить воспоминание. type: user | feedback | project | reference. tags — через запятую."""
    try:
        _request("POST", "/memories", body={
            "slug": slug, "type": type, "description": description,
            "body_md": body, "tags": [t.strip() for t in tags.split(",") if t.strip()],
        })
        return f"✅ Сохранено: {slug}"
    except Exception as e:
        return f"Ошибка: {e}"


@mcp.tool()
def memory_list(type: str = "", tags: str = "") -> str:
    """Список воспоминаний. Фильтры: type и/или tags через запятую."""
    try:
        params = {"limit": 100}
        if type: params["type"] = type
        if tags: params["tags"] = tags
        results = _request("GET", "/memories", params=params)
        if not results:
            return "Воспоминаний нет."
        lines = [f"- [{r['type']}] **{r['slug']}** — {r['description']}" for r in results]
        return f"Всего: {len(results)}\n\n" + "\n".join(lines)
    except Exception as e:
        return f"Ошибка: {e}"


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # SSE endpoint /sse и /messages требуют авторизацию
        auth = request.headers.get("Authorization", "")
        if MCP_TOKEN and (not auth.startswith("Bearer ") or auth[7:] != MCP_TOKEN):
            return Response("Unauthorized", status_code=401)
        return await call_next(request)


if __name__ == "__main__":
    from starlette.applications import Starlette
    from starlette.middleware import Middleware

    sse_app = mcp.sse_app()
    app = Starlette(middleware=[Middleware(BearerAuthMiddleware)])
    app.mount("/", sse_app)

    uvicorn.run(app, host="127.0.0.1", port=8766, log_level="warning")
