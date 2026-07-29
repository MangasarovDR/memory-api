# Чертоги Разума — Memory API

Единое хранилище памяти, задач и корпусов для всех клиентов Claude (Claude Code, Telegram-бот, Claude Desktop через MCP).

- **URL:** https://memory.automatiko.ru — порт `127.0.0.1:8765` за Caddy
- **Ключ:** `/opt/memory-api/.env` → `MEMORY_API_KEY` (никогда не выводить в stdout, подставлять inline)
- **Стек:** FastAPI + SQLite + sqlite-vec + fastembed, Docker
- **Данные:** `./data/memory.db`, модели эмбеддингов `./models/`

```bash
KEY="Authorization: Bearer $(grep MEMORY_API_KEY /opt/memory-api/.env | cut -d= -f2)"
curl -s "http://127.0.0.1:8765/health" -H "$KEY"
```

> **Кириллица в URL:** uvicorn отклоняет сырые не-ASCII байты в строке запроса
> с `Invalid HTTP request received` (HTTP 400). Всегда `curl -G --data-urlencode "q=..."`.

---

## Три подсистемы

| Раздел | Таблицы | Назначение |
|---|---|---|
| **Память** | `memories`, `memory_tags`, `memory_chunks`, `vec_chunks`, `memories_fts` | Долгосрочные знания, факты, проекты |
| **Задачи** | `todos`, `todo_tags` | Туду-лист с матрицей Эйзенхауэра |
| **Корпус** | `corpus_items`, `corpus_tags` | Накопление образцов (ссылки, тексты) |

---

## 1. Память

**6 типов:** `user | feedback | project | knowledge | idea | reference`
**importance 1–5:** ≥4 — всегда в контексте.
**Staleness:** project 30д, reference 60д, idea/knowledge 90д, user 180д, feedback 365д.

### Эндпоинты

```
GET    /memories                     список с фильтрами
GET    /memories/context?message=…   три уровня для инъекции в контекст
GET    /memories/search?q=…          mode=hybrid|fts|semantic
GET    /memories/{slug}              запись целиком
GET    /memories/stale               просроченные по свежести
POST   /memories                     создать ИЛИ перезаписать по slug
PUT    /memories/{slug}              частичное обновление
DELETE /memories/{slug}
DELETE /memories/expired
POST   /memories/embed-all           дозаполнить цельные векторы
POST   /memories/rechunk             перестроить чанки
```

> ⚠️ **`POST /memories` с существующим slug ПЕРЕЗАПИСЫВАЕТ `body_md` целиком**, а не дописывает.
> Для накопительных списков используйте раздел «Корпус», иначе данные потеряются.

### Чанкинг — почему он есть

Модель `paraphrase-multilingual-MiniLM-L12-v2` обрезает вход на **128 токенах**, что на кириллице ≈450 символов. Проверено замером: два текста с общим префиксом 500 символов и разными окончаниями дают косинус `1.000000` — идентичные векторы.

Срез `text[:2000]` в `embed_text()` — мёртвый код, модель до него не доходит. **Увеличивать его бессмысленно.**

До чанкинга в векторы попадало 14.1% базы (105 700 из 748 243 символов); записи по 20k+ были видны семантике на 2%.

**Решение:** `CHUNK_MAX_CHARS=400`, `CHUNK_OVERLAP=80`, режем по абзацам → предложениям → словам. Поиск идёт по чанкам, по каждой записи берётся лучший, `k` берётся с запасом (`want*8`) — иначе одна длинная запись вытеснит выдачу.

**Результат A/B** (запрос = дословное предложение из глубины записи, попадание в топ-5):

| | результат |
|---|---|
| цельный вектор | 20/67 (30%) |
| чанки | **61/67 (91%)** |

Регрессии нет: поиск по описанию топ-1 89% → 92%, топ-5 97% → 97%. Покрытие 14.1% → 100%.

`/memories/search` возвращает `matched_chunk` — совпавший фрагмент. Показывайте его вместо первых N символов записи.

```bash
# вся база заново (256 записей → 2901 чанк, ~3 минуты)
curl -s -X POST "http://127.0.0.1:8765/memories/rechunk?force=true" -H "$KEY"
# только записи без чанков
curl -s -X POST "http://127.0.0.1:8765/memories/rechunk" -H "$KEY"
```

> ⚠️ `vec_chunks` и `vec_memories` — **виртуальные** таблицы sqlite-vec.
> Каскад по внешнему ключу на них НЕ действует. Чистить через `_delete_chunks()`
> до удаления строк из `memory_chunks`, иначе останутся осиротевшие векторы.

`vec_memories` (цельный вектор на запись) сохраняется и обновляется — это путь отката без переиндексации.

### Известное ограничение

Модель слабовата для русского на **перефраз**-запросах: дистанции кучкуются в 3.2–3.6, различает плохо. На дословных совпадениях 91%. Чанки-«списки» (перечисления разнородных пунктов) размывают вектор — ограничение подхода, не бага. Гибридный режим частично компенсирует: FTS5 индексирует полный текст.

Кандидат на замену — `intfloat/multilingual-e5-large` (512 токенов, dim 1024, **2.24 ГБ против 0.22**). Решение отложено, см. туду id 21.

---

## 2. Задачи и матрица Эйзенхауэра

Две **независимые** оси: `important` и `urgent`. `NULL` = не разложено (осознанно отличается от «неважно/несрочно» — иначе новые задачи молча падали бы в мусорный квадрант).

| Квадрант | Значок | Действие |
|---|---|---|
| важное-срочное | 🔥 | Делать сейчас |
| важное-несрочное | 🎯 | Планировать |
| неважное-срочное | ⚡ | Делегировать |
| неважное-несрочное | 🗑 | Кандидат на выброс |
| не разложено | ❔ | Спросить Давида |

```
GET    /todos                    фильтры: status, date, no_date, tags, priority,
                                 important, urgent, unclassified
GET    /todos/today              просроченные / по времени / сегодня / без даты
GET    /todos/matrix             по квадрантам со счётчиками
GET    /todos/pending-reminders  сработавшие напоминания
POST   /todos                    создать
PATCH  /todos/{id}               изменить (в т.ч. important/urgent/repeat_days)
POST   /todos/{id}/done | /cancel | /start | /defer
DELETE /todos/{id}
```

### Повторяющиеся напоминания

Поле `repeat_days`: после срабатывания `remind_at` **сдвигается вперёд** на N дней, а не гасится. Без него напоминание одноразовое — сработает и замолчит навсегда.

---

## 3. Корпус образцов

Хранилище с **добавлением** записей — в отличие от `memories`, где повторный POST перезаписывает. Под проект «обучить Клода писать как живой человек».

```
POST   /corpus         добавить (UNIQUE по url — дубликат не создастся)
GET    /corpus         фильтры: status, platform, tags, limit, offset
GET    /corpus/stats   сводка по накоплению
GET    /corpus/{id}
PATCH  /corpus/{id}    в т.ч. status: new | analyzed | rejected
DELETE /corpus/{id}
```

Площадка определяется по домену автоматически (t.me, vk, dzen, habr, teletype, pikabu, medium, substack, lj…). Повторная присылка той же ссылки идемпотентна: дубликат не создаётся, уже собранные заметки не затираются, дополняются только пустые поля.

Поле `note` («чем именно нравится») — самое ценное, это вход для стадии анализа.

---

## Cron

| Когда | Что |
|---|---|
| 8:30 ежедневно | `scripts/morning_digest.py` — утренний дайджест в Telegram |
| каждые 5 минут | `scripts/reminder_check.py` — проверка напоминаний |

Оба ходят на `127.0.0.1:8765`, поэтому **не выявляют проблем публичного маршрута** — Telegram-бот ходит на публичный URL. Тестировать надо по публичному URL.

Дайджест показывает значок квадранта у каждой задачи; по понедельникам добавляет блок «важное, но несрочное» и счётчик неразложенных (без дублирования того, что уже выведено выше).

---

## Caddy: маршрутизация по префиксам

`/etc/caddy/Caddyfile`, блок `memory.automatiko.ru` проксирует **по префиксам**, а не целиком:

```
reverse_proxy /memories* 127.0.0.1:8765
reverse_proxy /todos*    127.0.0.1:8765
reverse_proxy /corpus*   127.0.0.1:8765
reverse_proxy /health    127.0.0.1:8765
reverse_proxy /mcp/*     127.0.0.1:8766
```

> ⚠️ **Забыли префикс — запрос отдаёт `HTTP 200` с ПУСТЫМ телом, а не 404.**
> Клиент на urllib падает с `'NoneType' object has no attribute 'get'` — диагноз
> неочевиден, легко списать на баг клиента. Добавили роуты в `app/main.py` —
> сразу добавьте `reverse_proxy`.

```bash
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && systemctl reload caddy
```

---

## Деплой

Код **вшит в образ** (`COPY app/ ./app/`), bind-mount только для `data/` и `models/`. Любая правка `app/main.py` требует пересборки:

```bash
cd /opt/memory-api
python3 -c "import ast;ast.parse(open('app/main.py').read())"   # синтаксис
docker compose build && docker compose up -d
sleep 15 && curl -s "http://127.0.0.1:8765/health" -H "$KEY"
```

Скрипты в `scripts/` запускаются с хоста через venv — пересборка не нужна.

**Перед изменениями схемы:** `cp data/memory.db /tmp/memory.db.bak-$(date +%F-%H%M)`

Миграции идемпотентны (`PRAGMA table_info` + `ALTER TABLE ADD COLUMN`), выполняются в `lifespan` при старте: `_init_db` → `_migrate_db` → `_init_todos_db` → `_init_corpus_db` → `_init_chunks_db`.
