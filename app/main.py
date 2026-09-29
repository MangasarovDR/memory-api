import os
import re
import json
import struct
import sqlite3
import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

import sqlite_vec
from fastembed import TextEmbedding
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, FileResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

logger = logging.getLogger(__name__)

DATABASE = os.environ.get("DATABASE_PATH", "/data/memory.db")
API_KEY  = os.environ.get("MEMORY_API_KEY", "")

MEDIA_ROOT = os.path.join(os.path.dirname(DATABASE), "media")
MEDIA_CATEGORIES = ["images", "audio", "video", "models", "vectors", "docs", "other"]

VALID_TYPES = {"user", "feedback", "project", "reference", "knowledge", "idea"}

# Days without update before a record is considered stale (per type)
STALE_THRESHOLDS = {
    "project":   30,
    "reference": 60,
    "idea":      90,
    "knowledge": 90,
    "user":      180,
    "feedback":  365,
}

EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
EMBED_DIM   = 384

_embed: Optional[TextEmbedding] = None

def get_embedder() -> TextEmbedding:
    global _embed
    if _embed is None:
        logger.info("Loading embedding model…")
        _embed = TextEmbedding(EMBED_MODEL)
        logger.info("Embedding model ready.")
    return _embed

def embed_text(text: str) -> bytes:
    """Return float32 vector as raw bytes for sqlite-vec."""
    model = get_embedder()
    vec = list(model.embed([text[:2000]]))[0]
    return struct.pack(f"{EMBED_DIM}f", *vec)


def embed_texts(texts: list[str]) -> list[bytes]:
    """Батчевый эмбеддинг — сильно быстрее поштучного при переиндексации."""
    if not texts:
        return []
    model = get_embedder()
    return [
        struct.pack(f"{EMBED_DIM}f", *v)
        for v in model.embed([t[:2000] for t in texts])
    ]


# ── Чанкинг ───────────────────────────────────────────────────────────────────
# Модель paraphrase-multilingual-MiniLM-L12-v2 обрезает вход на 128 токенах,
# что на кириллице ≈450 символов. Длинные записи целиком в вектор не влезают,
# поэтому режем их на куски и эмбеддим каждый отдельно.

CHUNK_MAX_CHARS = 400   # ~110 токенов на русском — с запасом под лимит 128
CHUNK_OVERLAP   = 80    # перехлёст, чтобы мысль не рвалась на границе
CHUNK_MAX_COUNT = 300   # предохранитель против аномально длинных записей


def _split_text(text: str) -> list[str]:
    """Режет текст на куски <= CHUNK_MAX_CHARS по границам абзацев и предложений."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= CHUNK_MAX_CHARS:
        return [text]

    # 1. Абзацы → 2. предложения → 3. жёстко по словам, если предложение-монстр
    pieces: list[str] = []
    for para in (p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()):
        if len(para) <= CHUNK_MAX_CHARS:
            pieces.append(para)
            continue
        for sent in re.split(r"(?<=[.!?;:])\s+", para):
            sent = sent.strip()
            if not sent:
                continue
            if len(sent) <= CHUNK_MAX_CHARS:
                pieces.append(sent)
                continue
            cur = ""
            for word in sent.split():
                if len(cur) + len(word) + 1 > CHUNK_MAX_CHARS:
                    if cur:
                        pieces.append(cur)
                    cur = word[:CHUNK_MAX_CHARS]
                else:
                    cur = f"{cur} {word}".strip()
            if cur:
                pieces.append(cur)

    # Склеиваем мелкие куски обратно до целевого размера, с перехлёстом
    chunks: list[str] = []
    cur = ""
    for piece in pieces:
        candidate = piece if not cur else f"{cur}\n{piece}"
        if len(candidate) <= CHUNK_MAX_CHARS:
            cur = candidate
            continue
        if cur:
            chunks.append(cur)
            if len(chunks) >= CHUNK_MAX_COUNT:
                return chunks
            tail = cur[-CHUNK_OVERLAP:].lstrip()
            cur = f"{tail}\n{piece}" if len(tail) + len(piece) + 1 <= CHUNK_MAX_CHARS else piece
        else:
            cur = piece
    if cur:
        chunks.append(cur)
    return chunks[:CHUNK_MAX_COUNT]


def _init_chunks_db():
    conn = _connect()
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS memory_chunks (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            chunk_idx INTEGER NOT NULL,
            text      TEXT    NOT NULL,
            UNIQUE(memory_id, chunk_idx)
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_memory ON memory_chunks(memory_id);

        CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
            embedding float[{EMBED_DIM}]
        );
    """)
    conn.commit()
    conn.close()


def _delete_chunks(conn: sqlite3.Connection, memory_id: int):
    """vec_chunks — виртуальная таблица, каскад по FK на неё не действует."""
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM memory_chunks WHERE memory_id = ?", (memory_id,)
    ).fetchall()]
    if ids:
        conn.execute(f"DELETE FROM vec_chunks WHERE rowid IN ({','.join('?' * len(ids))})", ids)
        conn.execute("DELETE FROM memory_chunks WHERE memory_id = ?", (memory_id,))


def _upsert_chunks(conn: sqlite3.Connection, memory_id: int, text: str) -> int:
    """Перерезает запись на чанки и переэмбеддит их. Возвращает число чанков."""
    try:
        _delete_chunks(conn, memory_id)
        chunks = _split_text(text)
        if not chunks:
            return 0
        vectors = embed_texts(chunks)
        for idx, (chunk, vec) in enumerate(zip(chunks, vectors)):
            cur = conn.execute(
                "INSERT INTO memory_chunks(memory_id, chunk_idx, text) VALUES (?,?,?)",
                (memory_id, idx, chunk),
            )
            conn.execute(
                "INSERT INTO vec_chunks(rowid, embedding) VALUES (?,?)",
                (cur.lastrowid, vec),
            )
        return len(chunks)
    except Exception as e:
        logger.warning(f"Chunking failed for id={memory_id}: {e}")
        return 0


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(os.path.dirname(DATABASE), exist_ok=True)
    for cat in MEDIA_CATEGORIES:
        os.makedirs(os.path.join(MEDIA_ROOT, cat), exist_ok=True)
    _init_db()
    _migrate_db()
    _init_todos_db()
    _init_corpus_db()
    _init_chunks_db()
    # Warm up embedder so first request is instant
    try:
        get_embedder()
    except Exception as e:
        logger.warning(f"Embedder warmup failed (will retry on first request): {e}")
    yield

app = FastAPI(title="Чертоги Разума API", version="2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://claude.ai", "http://localhost:3000", "http://localhost:8000"],
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# ── Auth ──────────────────────────────────────────────────────────────────────

_security = HTTPBearer()

def require_auth(credentials: HTTPAuthorizationCredentials = Depends(_security)):
    if not API_KEY:
        raise HTTPException(status_code=500, detail="MEMORY_API_KEY not configured")
    if credentials.credentials != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

# ── Database ──────────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    sqlite_vec.load(conn)
    return conn

def get_db():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()

def _init_db():
    conn = _connect()
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS memories (
            id          INTEGER   PRIMARY KEY AUTOINCREMENT,
            slug        TEXT      UNIQUE NOT NULL,
            type        TEXT      NOT NULL
                        CHECK(type IN ('user','feedback','project','reference','knowledge','idea')),
            description TEXT      NOT NULL,
            body_md     TEXT      NOT NULL,
            importance  INTEGER   DEFAULT 3 CHECK(importance BETWEEN 1 AND 5),
            source      TEXT      DEFAULT 'manual',
            expires_at  TIMESTAMP,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_memories_type       ON memories(type);
        CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance);

        CREATE TABLE IF NOT EXISTS memory_tags (
            memory_id  INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
            tag        TEXT    NOT NULL,
            PRIMARY KEY (memory_id, tag)
        );
        CREATE INDEX IF NOT EXISTS idx_tag     ON memory_tags(tag);
        CREATE INDEX IF NOT EXISTS idx_tag_mem ON memory_tags(memory_id);

        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            slug, description, body_md,
            content=memories, content_rowid=id
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS vec_memories USING vec0(
            rowid INTEGER PRIMARY KEY,
            embedding FLOAT[{EMBED_DIM}]
        );

        CREATE TRIGGER IF NOT EXISTS fts_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, slug, description, body_md)
            VALUES (new.id, new.slug, new.description, new.body_md);
        END;
        CREATE TRIGGER IF NOT EXISTS fts_ad AFTER DELETE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, slug, description, body_md)
            VALUES ('delete', old.id, old.slug, old.description, old.body_md);
        END;
        CREATE TRIGGER IF NOT EXISTS fts_au AFTER UPDATE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, slug, description, body_md)
            VALUES ('delete', old.id, old.slug, old.description, old.body_md);
            INSERT INTO memories_fts(rowid, slug, description, body_md)
            VALUES (new.id, new.slug, new.description, new.body_md);
        END;
        CREATE TRIGGER IF NOT EXISTS updated_at_trigger AFTER UPDATE ON memories BEGIN
            UPDATE memories SET updated_at = CURRENT_TIMESTAMP WHERE id = new.id;
        END;
    """)
    conn.commit()
    conn.close()

def _migrate_db():
    """Idempotent schema upgrade to v2."""
    conn = _connect()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}

    if "importance" not in cols:
        conn.execute("PRAGMA foreign_keys=OFF")
        for trig in ["fts_ai","fts_ad","fts_au","updated_at","updated_at_trigger"]:
            conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
        conn.execute("DROP TABLE IF EXISTS memories_fts")
        conn.execute("DROP TABLE IF EXISTS vec_memories")
        conn.execute("ALTER TABLE memories RENAME TO memories_old")
        conn.execute(f"""
            CREATE TABLE memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT UNIQUE NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('user','feedback','project','reference','knowledge','idea')),
                description TEXT NOT NULL, body_md TEXT NOT NULL,
                importance INTEGER DEFAULT 3 CHECK(importance BETWEEN 1 AND 5),
                source TEXT DEFAULT 'legacy', expires_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            INSERT INTO memories(id,slug,type,description,body_md,importance,source,created_at,updated_at)
            SELECT id,slug,type,description,body_md,3,'legacy',created_at,updated_at FROM memories_old
        """)
        conn.execute("DROP TABLE memories_old")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance)")
        conn.execute(f"""
            CREATE VIRTUAL TABLE memories_fts USING fts5(
                slug, description, body_md, content=memories, content_rowid=id)
        """)
        conn.execute(f"""
            CREATE VIRTUAL TABLE vec_memories USING vec0(
                rowid INTEGER PRIMARY KEY, embedding FLOAT[{EMBED_DIM}])
        """)
        conn.execute("INSERT INTO memories_fts(rowid,slug,description,body_md) SELECT id,slug,description,body_md FROM memories")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.commit()
        for sql in [
            f"""CREATE TRIGGER fts_ai AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(rowid,slug,description,body_md)
                VALUES(new.id,new.slug,new.description,new.body_md); END""",
            f"""CREATE TRIGGER fts_ad AFTER DELETE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts,rowid,slug,description,body_md)
                VALUES('delete',old.id,old.slug,old.description,old.body_md); END""",
            f"""CREATE TRIGGER fts_au AFTER UPDATE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts,rowid,slug,description,body_md)
                VALUES('delete',old.id,old.slug,old.description,old.body_md);
                INSERT INTO memories_fts(rowid,slug,description,body_md)
                VALUES(new.id,new.slug,new.description,new.body_md); END""",
            """CREATE TRIGGER updated_at_trigger AFTER UPDATE ON memories BEGIN
                UPDATE memories SET updated_at=CURRENT_TIMESTAMP WHERE id=new.id; END""",
        ]:
            conn.execute(sql)
        conn.execute("UPDATE memories SET importance=5 WHERE type='feedback'")
        conn.execute("UPDATE memories SET importance=4 WHERE type IN ('user','project')")
        conn.commit()

    # Ensure vec_memories table exists (in case of partial migration)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "vec_memories" not in tables:
        conn.execute(f"""
            CREATE VIRTUAL TABLE vec_memories USING vec0(
                rowid INTEGER PRIMARY KEY, embedding FLOAT[{EMBED_DIM}])
        """)
        conn.commit()

    # v2.1: optional media attachment pointer (file lives on disk/remote, not in SQLite)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
    if "media_path" not in cols:
        conn.execute("ALTER TABLE memories ADD COLUMN media_path TEXT")
        conn.execute("ALTER TABLE memories ADD COLUMN media_type TEXT")
        conn.execute("ALTER TABLE memories ADD COLUMN media_bytes INTEGER")
        conn.commit()

    conn.close()

# ── Helpers ───────────────────────────────────────────────────────────────────

def _row_to_dict(row: sqlite3.Row, conn: sqlite3.Connection) -> dict:
    d = dict(row)
    d.pop("rank", None)
    tags = conn.execute(
        "SELECT tag FROM memory_tags WHERE memory_id = ? ORDER BY tag", (d["id"],)
    ).fetchall()
    d["tags"] = [t["tag"] for t in tags]
    return d

def _upsert_tags(conn: sqlite3.Connection, memory_id: int, tags: list[str]):
    conn.execute("DELETE FROM memory_tags WHERE memory_id = ?", (memory_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO memory_tags (memory_id, tag) VALUES (?, ?)",
        [(memory_id, t.strip().lower()) for t in tags if t.strip()],
    )

def _upsert_embedding(conn: sqlite3.Connection, memory_id: int, text: str):
    try:
        vec = embed_text(text)
        conn.execute(
            "INSERT OR REPLACE INTO vec_memories(rowid, embedding) VALUES (?, ?)",
            (memory_id, vec),
        )
    except Exception as e:
        logger.warning(f"Embedding failed for id={memory_id}: {e}")

def _embed_text_for_search(text: str) -> bytes:
    return embed_text(text)


def _semantic_search(
    db: sqlite3.Connection,
    query: str,
    want: int,
    type_filter: Optional[str] = None,
    exclude: Optional[set[int]] = None,
) -> list[dict]:
    """
    Семантический поиск по чанкам. Одна запись даёт много чанков, поэтому
    берём по каждой записи ЛУЧШИЙ (минимальная дистанция) и ранжируем по нему.
    k берём с запасом — иначе одна длинная запись вытеснит всё остальное.
    """
    exclude = exclude or set()
    k = min(max(want * 8, 40), 500)
    rows = db.execute(
        f"""SELECT c.memory_id AS mid, c.id AS cid, v.distance AS dist
            FROM vec_chunks v
            JOIN memory_chunks c ON c.id = v.rowid
            WHERE v.embedding MATCH ? AND k = {k}
            ORDER BY v.distance""",
        (_embed_text_for_search(query),),
    ).fetchall()

    best: dict[int, tuple[float, int]] = {}   # memory_id -> (дистанция, chunk_id)
    for r in rows:
        mid, dist, cid = r["mid"], r["dist"], r["cid"]
        if mid not in best or dist < best[mid][0]:
            best[mid] = (dist, cid)

    out: list[dict] = []
    for mid, (dist, cid) in sorted(best.items(), key=lambda kv: kv[1][0]):
        if mid in exclude:
            continue
        row = db.execute("SELECT * FROM memories WHERE id = ?", (mid,)).fetchone()
        if not row:
            continue
        if type_filter and row["type"] != type_filter:
            continue
        item = _row_to_dict(row, db)
        item["distance"] = dist
        # Фрагмент, который реально совпал — полезнее первых N символов записи
        chunk = db.execute("SELECT text FROM memory_chunks WHERE id = ?", (cid,)).fetchone()
        item["matched_chunk"] = chunk["text"] if chunk else None
        out.append(item)
        if len(out) >= want:
            break
    return out

# ── Pydantic models ───────────────────────────────────────────────────────────

class MemoryIn(BaseModel):
    slug:        str
    type:        str
    description: str
    tags:        list[str]     = []
    body_md:     str
    importance:  int           = 3
    source:      str           = "manual"
    expires_at:  Optional[str] = None
    media_path:  Optional[str] = None   # relative path under MEDIA_ROOT, or a remote URL
    media_type:  Optional[str] = None   # MIME type, e.g. "image/png", "model/stl"
    media_bytes: Optional[int] = None

class MemoryPatch(BaseModel):
    type:        Optional[str]       = None
    description: Optional[str]       = None
    tags:        Optional[list[str]] = None
    body_md:     Optional[str]       = None
    importance:  Optional[int]       = None
    source:      Optional[str]       = None
    expires_at:  Optional[str]       = None
    media_path:  Optional[str]       = None
    media_type:  Optional[str]       = None
    media_bytes: Optional[int]       = None

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    conn = _connect()
    count  = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    vcount = conn.execute("SELECT COUNT(*) FROM vec_memories").fetchone()[0]
    try:
        chunks  = conn.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0]
        chunked = conn.execute("SELECT COUNT(DISTINCT memory_id) FROM memory_chunks").fetchone()[0]
    except sqlite3.OperationalError:
        chunks = chunked = 0
    conn.close()
    return {
        "status": "ok", "records": count, "embedded": vcount,
        "chunks": chunks, "chunked_records": chunked, "version": "2.1",
    }


@app.get("/memories", dependencies=[Depends(require_auth)])
def list_memories(
    type:   Optional[str] = None,
    tags:   Optional[str] = None,
    source: Optional[str] = None,
    limit:  int           = Query(200, le=1000),
    db:     sqlite3.Connection = Depends(get_db),
):
    conditions, params = [], []
    if type:
        conditions.append("m.type = ?"); params.append(type)
    if source:
        conditions.append("m.source = ?"); params.append(source)
    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        ph = ",".join("?" * len(tag_list))
        conditions.append(f"EXISTS (SELECT 1 FROM memory_tags t WHERE t.memory_id=m.id AND t.tag IN ({ph}))")
        params.extend(tag_list)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    rows = db.execute(
        f"SELECT m.* FROM memories m {where} ORDER BY m.importance DESC, m.updated_at DESC LIMIT ?",
        params,
    ).fetchall()
    return [_row_to_dict(r, db) for r in rows]


@app.get("/memories/export", dependencies=[Depends(require_auth)], response_class=PlainTextResponse)
def export_memories(db: sqlite3.Connection = Depends(get_db)):
    rows = db.execute("SELECT * FROM memories ORDER BY type, importance DESC, slug").fetchall()
    sections: dict[str, list[str]] = {t: [] for t in sorted(VALID_TYPES)}
    for r in rows:
        d = _row_to_dict(r, db)
        imp = "★" * d["importance"]
        sections[d["type"]].append(f"- [{d['slug']}] {imp} — {d['description']}")
    lines = ["# Чертоги Разума — Индекс\n"]
    for t, entries in sections.items():
        if entries:
            lines.append(f"## {t} ({len(entries)})")
            lines.extend(entries)
            lines.append("")
    return "\n".join(lines)


# Сколько мест в ответе всегда оставляем второму и третьему ярусу, когда в
# запросе есть текст. Без этой брони always-load съедал ответ целиком: ярусов
# было три, а работал один. 22.08.2026 при потолке 20 и 221 записи importance>=4
# семантика не получала НИ ОДНОГО места — поиск был мёртв, и заметить это было
# нельзя, потому что ответ выглядел полным.
SEARCH_RESERVE = 10   # при потолке 100 и ярусе ~89 обрезать не приходится вовсе:
                      # поиск получает свои места из остатка, а не за чей-то счёт


@app.get("/memories/context", dependencies=[Depends(require_auth)])
def smart_context(
    message: str = Query(""),
    limit:   int = Query(100, le=200),
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Three-tier retrieval:
    1. Always-load: importance >= 4 (core profile + rules)
    2. Semantic: ANN search via vec_memories (if embedder ready)
    3. Keyword FTS5 fallback / supplement

    Если always-load не влезает в потолок, лишнее отрезается — но по СВЕЖЕСТИ,
    а не как придётся. Ответ всегда сообщает `always_total` и `truncated`:
    молча терять записи, объявленные «всегда в контексте», нельзя — именно так
    это и пряталось до 22.08.2026.
    """
    # Tier 1 — always load. Порядок решает, кто уцелеет при обрезке: сначала
    # важность, потом свежесть. Прежняя сортировка по типу отрезала произвольно.
    always_rows = db.execute(
        "SELECT * FROM memories WHERE importance >= 4 "
        "ORDER BY importance DESC, updated_at DESC"
    ).fetchall()
    seen_ids = {r["id"] for r in always_rows}

    keyword_rows, semantic_rows = [], []

    if message.strip():
        # Tier 2 — semantic search (по чанкам)
        try:
            semantic_rows = _semantic_search(
                db, message, want=15, exclude=seen_ids
            )
        except Exception as e:
            logger.warning(f"Semantic search failed: {e}")

        # Tier 3 — FTS5 keyword (supplement / fallback)
        try:
            fts_rows = db.execute(
                """SELECT m.*, rank FROM memories_fts
                   JOIN memories m ON m.id = memories_fts.rowid
                   WHERE memories_fts MATCH ? ORDER BY rank LIMIT 10""",
                (message,),
            ).fetchall()
            fts_ids = {r["id"] for r in semantic_rows} | seen_ids
            keyword_rows = [r for r in fts_rows if r["id"] not in fts_ids]
        except sqlite3.OperationalError:
            pass

    always_total = len(always_rows)
    # Броню отдаём поиску только когда есть что искать: на пустом сообщении
    # ярусы 2 и 3 пусты, и резервировать под них места значит выбросить
    # always-load ни за чем.
    reserve = SEARCH_RESERVE if (message.strip() and (semantic_rows or keyword_rows)) else 0
    keep = max(limit - reserve, 0) if always_total > limit - reserve else always_total

    combined  = [_row_to_dict(r, db) for r in always_rows[:keep]]
    budget = limit - len(combined)
    combined += semantic_rows[:max(0, budget - 3)]   # уже словари из _semantic_search
    combined += [_row_to_dict(r, db) for r in keyword_rows[:3]]
    combined = combined[:limit]
    dropped = always_total - keep
    if dropped:
        logger.warning(
            "context: always-load %d записей при потолке %d, отрезано %d "
            "(проредить importance — /opt/memory-api/scripts/prune_importance.py)",
            always_total, limit, dropped)
    return {"memories": combined, "total": len(combined),
            "always_total": always_total, "truncated": dropped}


@app.get("/memories/search", dependencies=[Depends(require_auth)])
def search_memories(
    q:      str = Query(..., min_length=1),
    type:   Optional[str] = None,
    mode:   str = Query("hybrid", pattern="^(fts|semantic|hybrid)$"),
    limit:  int = Query(20, le=100),
    db: sqlite3.Connection = Depends(get_db),
):
    results = []
    seen_ids: set[int] = set()

    if mode in ("semantic", "hybrid"):
        try:
            for item in _semantic_search(db, q, want=limit, type_filter=type):
                results.append(item)
                seen_ids.add(item["id"])
        except Exception as e:
            logger.warning(f"Semantic search error: {e}")

    if mode in ("fts", "hybrid"):
        try:
            rows = db.execute(
                """SELECT m.*, rank FROM memories_fts
                   JOIN memories m ON m.id = memories_fts.rowid
                   WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?""",
                (q, limit * 2),
            ).fetchall()
            for r in rows:
                if r["id"] in seen_ids:
                    continue
                if type and r["type"] != type:
                    continue
                results.append(_row_to_dict(r, db))
        except sqlite3.OperationalError as e:
            if mode == "fts":
                raise HTTPException(status_code=400, detail=str(e))

    return results[:limit]


@app.get("/memories/{slug}", dependencies=[Depends(require_auth)])
def get_memory(slug: str, db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT * FROM memories WHERE slug = ?", (slug,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return _row_to_dict(row, db)


@app.get("/memories/{slug}/media", dependencies=[Depends(require_auth)])
def get_memory_media(slug: str, db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT media_path FROM memories WHERE slug = ?", (slug,)).fetchone()
    if not row or not row["media_path"]:
        raise HTTPException(status_code=404, detail="No media attached")
    path = row["media_path"]
    if path.startswith("http://") or path.startswith("https://"):
        return RedirectResponse(path)
    full = os.path.realpath(os.path.join(MEDIA_ROOT, path))
    if os.path.commonpath([full, MEDIA_ROOT]) != MEDIA_ROOT or not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="Media file missing on disk")
    return FileResponse(full)


@app.post("/memories", dependencies=[Depends(require_auth)], status_code=201)
def upsert_memory(payload: MemoryIn, db: sqlite3.Connection = Depends(get_db)):
    if payload.type not in VALID_TYPES:
        raise HTTPException(status_code=422, detail=f"type must be one of {sorted(VALID_TYPES)}")
    if not (1 <= payload.importance <= 5):
        raise HTTPException(status_code=422, detail="importance must be 1–5")

    embed_input = f"{payload.description}\n\n{payload.body_md}"

    existing = db.execute("SELECT id FROM memories WHERE slug = ?", (payload.slug,)).fetchone()
    if existing:
        db.execute(
            """UPDATE memories SET type=?,description=?,body_md=?,importance=?,source=?,expires_at=?,
               media_path=?,media_type=?,media_bytes=? WHERE slug=?""",
            (payload.type, payload.description, payload.body_md,
             payload.importance, payload.source, payload.expires_at,
             payload.media_path, payload.media_type, payload.media_bytes, payload.slug),
        )
        memory_id = existing["id"]
    else:
        cur = db.execute(
            """INSERT INTO memories(slug,type,description,body_md,importance,source,expires_at,
               media_path,media_type,media_bytes) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (payload.slug, payload.type, payload.description, payload.body_md,
             payload.importance, payload.source, payload.expires_at,
             payload.media_path, payload.media_type, payload.media_bytes),
        )
        memory_id = cur.lastrowid

    _upsert_tags(db, memory_id, payload.tags)
    _upsert_embedding(db, memory_id, embed_input)   # цельный вектор — совместимость
    _upsert_chunks(db, memory_id, embed_input)      # чанки — то, по чему ищем
    db.commit()
    return _row_to_dict(db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone(), db)


@app.put("/memories/{slug}", dependencies=[Depends(require_auth)])
def patch_memory(slug: str, payload: MemoryPatch, db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT * FROM memories WHERE slug = ?", (slug,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    if payload.type and payload.type not in VALID_TYPES:
        raise HTTPException(status_code=422, detail=f"type must be one of {sorted(VALID_TYPES)}")

    fields = {k: v for k, v in payload.model_dump().items() if v is not None and k != "tags"}
    if fields:
        set_clause = ", ".join(f"{k}=?" for k in fields)
        db.execute(f"UPDATE memories SET {set_clause} WHERE slug=?", [*fields.values(), slug])
    if payload.tags is not None:
        _upsert_tags(db, row["id"], payload.tags)

    if payload.body_md or payload.description:
        updated = db.execute("SELECT * FROM memories WHERE slug=?", (slug,)).fetchone()
        fresh = f"{updated['description']}\n\n{updated['body_md']}"
        _upsert_embedding(db, row["id"], fresh)
        _upsert_chunks(db, row["id"], fresh)

    db.commit()
    return _row_to_dict(db.execute("SELECT * FROM memories WHERE slug = ?", (slug,)).fetchone(), db)


@app.delete("/memories/{slug}", dependencies=[Depends(require_auth)], status_code=204)
def delete_memory(slug: str, db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT id FROM memories WHERE slug = ?", (slug,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    _delete_chunks(db, row["id"])
    db.execute("DELETE FROM vec_memories WHERE rowid = ?", (row["id"],))
    db.execute("DELETE FROM memories WHERE slug = ?", (slug,))
    db.commit()


@app.get("/memories/stale", dependencies=[Depends(require_auth)])
def stale_memories(db: sqlite3.Connection = Depends(get_db)):
    """Records not updated within their type's staleness threshold."""
    results = []
    for mem_type, days in STALE_THRESHOLDS.items():
        rows = db.execute(
            """SELECT *,
                      CAST(julianday('now') - julianday(updated_at) AS INTEGER) AS days_old
               FROM memories
               WHERE type = ? AND datetime(updated_at) < datetime('now', ?)
               ORDER BY updated_at ASC""",
            (mem_type, f"-{days} days"),
        ).fetchall()
        for r in rows:
            d = _row_to_dict(r, db)
            d["days_old"]        = dict(r).get("days_old", 0)
            d["threshold_days"]  = days
            results.append(d)
    return sorted(results, key=lambda x: x.get("days_old", 0), reverse=True)


@app.delete("/memories/expired", dependencies=[Depends(require_auth)])
def purge_expired(db: sqlite3.Connection = Depends(get_db)):
    """Delete records whose expires_at is in the past."""
    result = db.execute(
        "SELECT id FROM memories WHERE expires_at IS NOT NULL AND datetime(expires_at) < datetime('now')"
    ).fetchall()
    ids = [r["id"] for r in result]
    if ids:
        for mid in ids:
            _delete_chunks(db, mid)
        db.execute(f"DELETE FROM vec_memories WHERE rowid IN ({','.join('?'*len(ids))})", ids)
        db.execute(f"DELETE FROM memories WHERE id IN ({','.join('?'*len(ids))})", ids)
        db.commit()
    return {"purged": len(ids)}


# ── Todos DB ──────────────────────────────────────────────────────────────────

def _init_todos_db():
    conn = _connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS todos (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            title        TEXT    NOT NULL,
            description  TEXT    DEFAULT '',
            status       TEXT    DEFAULT 'pending'
                        CHECK(status IN ('pending','in_progress','done','cancelled')),
            priority     INTEGER DEFAULT 3 CHECK(priority BETWEEN 1 AND 5),
            due_at       TEXT,
            has_time     INTEGER DEFAULT 0,
            remind_at    TEXT,
            notes        TEXT    DEFAULT '',
            created_at   TEXT    DEFAULT (datetime('now')),
            updated_at   TEXT    DEFAULT (datetime('now')),
            started_at   TEXT,
            completed_at TEXT,
            cancelled_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_todos_status ON todos(status);
        CREATE INDEX IF NOT EXISTS idx_todos_due_at ON todos(due_at);
        CREATE INDEX IF NOT EXISTS idx_todos_remind ON todos(remind_at);

        CREATE TABLE IF NOT EXISTS todo_tags (
            todo_id INTEGER NOT NULL REFERENCES todos(id) ON DELETE CASCADE,
            tag     TEXT    NOT NULL,
            PRIMARY KEY (todo_id, tag)
        );
        CREATE INDEX IF NOT EXISTS idx_todo_tags ON todo_tags(tag);

        CREATE TRIGGER IF NOT EXISTS todos_updated_at AFTER UPDATE ON todos BEGIN
            UPDATE todos SET updated_at = datetime('now') WHERE id = new.id;
        END;
    """)
    # Матрица Эйзенхауэра: важность и срочность — независимые оси.
    # NULL = не классифицировано (осознанно отличаем от «неважно/несрочно»).
    cols = {r[1] for r in conn.execute("PRAGMA table_info(todos)").fetchall()}
    if "important" not in cols:
        conn.execute("ALTER TABLE todos ADD COLUMN important INTEGER")
        conn.execute("ALTER TABLE todos ADD COLUMN urgent INTEGER")
    # Повторяющееся напоминание: после срабатывания remind_at сдвигается
    # на repeat_days вперёд, а не гасится. Для дел, о которых нужно
    # напоминать периодически, пока они не закрыты.
    if "repeat_days" not in cols:
        conn.execute("ALTER TABLE todos ADD COLUMN repeat_days INTEGER")
    conn.commit()
    conn.close()


# Квадранты матрицы Эйзенхауэра
QUADRANTS = {
    (1, 1): ("важное-срочное",     "🔥", "Делать сейчас"),
    (1, 0): ("важное-несрочное",   "🎯", "Планировать"),
    (0, 1): ("неважное-срочное",   "⚡", "Делегировать или быстро закрыть"),
    (0, 0): ("неважное-несрочное", "🗑", "Кандидат на выброс"),
}


def _quadrant_of(row: dict) -> Optional[dict]:
    imp, urg = row.get("important"), row.get("urgent")
    if imp is None or urg is None:
        return None
    slug, icon, action = QUADRANTS[(1 if imp else 0, 1 if urg else 0)]
    return {"key": slug, "icon": icon, "action": action,
            "important": bool(imp), "urgent": bool(urg)}

def _todo_tags_upsert(conn: sqlite3.Connection, todo_id: int, tags: list[str]):
    conn.execute("DELETE FROM todo_tags WHERE todo_id = ?", (todo_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO todo_tags(todo_id, tag) VALUES (?, ?)",
        [(todo_id, t.strip().lower()) for t in tags if t.strip()],
    )

def _todo_to_dict(row: sqlite3.Row, conn: sqlite3.Connection) -> dict:
    d = dict(row)
    tags = conn.execute(
        "SELECT tag FROM todo_tags WHERE todo_id = ? ORDER BY tag", (d["id"],)
    ).fetchall()
    d["tags"] = [t["tag"] for t in tags]
    d["quadrant"] = _quadrant_of(d)
    return d

# ── Todo Pydantic models ───────────────────────────────────────────────────────

class TodoIn(BaseModel):
    title:       str
    description: str            = ""
    priority:    int            = 3
    due_at:      Optional[str]  = None
    has_time:    int            = 0
    remind_at:   Optional[str]  = None
    tags:        list[str]      = []
    notes:       str            = ""
    important:   Optional[bool] = None
    urgent:      Optional[bool] = None
    repeat_days: Optional[int]  = None

class TodoPatch(BaseModel):
    title:       Optional[str]       = None
    description: Optional[str]       = None
    priority:    Optional[int]       = None
    due_at:      Optional[str]       = None
    has_time:    Optional[int]       = None
    remind_at:   Any                 = "__unset__"
    tags:        Optional[list[str]] = None
    notes:       Optional[str]       = None
    important:   Optional[bool]      = None
    urgent:      Optional[bool]      = None
    repeat_days: Optional[int]       = None

class TodoDeferBody(BaseModel):
    to:        str
    has_time:  int           = 0
    remind_at: Optional[str] = None
    notes:     Optional[str] = None

class TodoActionNotes(BaseModel):
    notes: Optional[str] = None

# ── Todo Routes ────────────────────────────────────────────────────────────────

@app.get("/todos", dependencies=[Depends(require_auth)])
def list_todos(
    status:    Optional[str]  = None,
    date:      Optional[str]  = None,
    no_date:   bool           = False,
    tags:      Optional[str]  = None,
    priority:  Optional[int]  = None,
    important: Optional[bool] = None,
    urgent:    Optional[bool] = None,
    unclassified: bool        = False,
    limit:     int            = Query(100, le=500),
    db: sqlite3.Connection    = Depends(get_db),
):
    conditions, params = [], []
    if status:
        conditions.append("status = ?"); params.append(status)
    if important is not None:
        conditions.append("important = ?"); params.append(int(important))
    if urgent is not None:
        conditions.append("urgent = ?"); params.append(int(urgent))
    if unclassified:
        conditions.append("(important IS NULL OR urgent IS NULL)")
    if date:
        conditions.append("due_at LIKE ?"); params.append(f"{date}%")
    if no_date:
        conditions.append("due_at IS NULL")
    if priority:
        conditions.append("priority = ?"); params.append(priority)
    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        ph = ",".join("?" * len(tag_list))
        conditions.append(
            f"EXISTS (SELECT 1 FROM todo_tags tt WHERE tt.todo_id=todos.id AND tt.tag IN ({ph}))"
        )
        params.extend(tag_list)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    rows = db.execute(
        f"SELECT * FROM todos {where} ORDER BY priority DESC, due_at ASC NULLS LAST, id DESC LIMIT ?",
        params,
    ).fetchall()
    return [_todo_to_dict(r, db) for r in rows]


@app.get("/todos/today", dependencies=[Depends(require_auth)])
def todos_today(db: sqlite3.Connection = Depends(get_db)):
    today = db.execute("SELECT date('now')").fetchone()[0]

    scheduled = db.execute(
        "SELECT * FROM todos WHERE due_at LIKE ? AND has_time=1 AND status NOT IN ('done','cancelled') ORDER BY due_at ASC",
        (f"{today}%",),
    ).fetchall()

    today_no_time = db.execute(
        "SELECT * FROM todos WHERE due_at LIKE ? AND has_time=0 AND status NOT IN ('done','cancelled') ORDER BY priority DESC",
        (f"{today}%",),
    ).fetchall()

    overdue = db.execute(
        "SELECT * FROM todos WHERE due_at IS NOT NULL AND due_at < ? AND status NOT IN ('done','cancelled') ORDER BY due_at ASC",
        (today,),
    ).fetchall()

    anytime = db.execute(
        "SELECT * FROM todos WHERE due_at IS NULL AND status NOT IN ('done','cancelled') ORDER BY priority DESC, id DESC",
    ).fetchall()

    return {
        "date": today,
        "scheduled":    [_todo_to_dict(r, db) for r in scheduled],
        "today_no_time":[_todo_to_dict(r, db) for r in today_no_time],
        "overdue":      [_todo_to_dict(r, db) for r in overdue],
        "anytime":      [_todo_to_dict(r, db) for r in anytime],
    }


@app.get("/todos/matrix", dependencies=[Depends(require_auth)])
def todos_matrix(db: sqlite3.Connection = Depends(get_db)):
    """Открытые задачи, разложенные по матрице Эйзенхауэра."""
    rows = db.execute(
        """SELECT * FROM todos WHERE status NOT IN ('done','cancelled')
           ORDER BY priority DESC, due_at ASC NULLS LAST, id DESC"""
    ).fetchall()

    buckets: dict[str, list] = {slug: [] for slug, _, _ in QUADRANTS.values()}
    buckets["неклассифицировано"] = []

    for r in rows:
        item = _todo_to_dict(r, db)
        q = item["quadrant"]
        buckets[q["key"] if q else "неклассифицировано"].append(item)

    meta = {slug: {"icon": icon, "action": action}
            for slug, icon, action in QUADRANTS.values()}
    return {
        "quadrants": buckets,
        "meta": meta,
        "counts": {k: len(v) for k, v in buckets.items()},
        "total_open": len(rows),
    }


@app.get("/todos/pending-reminders", dependencies=[Depends(require_auth)])
def pending_reminders(db: sqlite3.Connection = Depends(get_db)):
    rows = db.execute(
        """SELECT * FROM todos
           WHERE remind_at IS NOT NULL
             AND remind_at <= datetime('now')
             AND status NOT IN ('done','cancelled')
           ORDER BY remind_at ASC""",
    ).fetchall()
    return [_todo_to_dict(r, db) for r in rows]


@app.post("/todos", dependencies=[Depends(require_auth)], status_code=201)
def create_todo(payload: TodoIn, db: sqlite3.Connection = Depends(get_db)):
    if not (1 <= payload.priority <= 5):
        raise HTTPException(status_code=422, detail="priority must be 1–5")
    cur = db.execute(
        """INSERT INTO todos(title,description,priority,due_at,has_time,remind_at,notes,
                             important,urgent,repeat_days)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (payload.title, payload.description, payload.priority,
         payload.due_at, payload.has_time, payload.remind_at, payload.notes,
         None if payload.important is None else int(payload.important),
         None if payload.urgent    is None else int(payload.urgent),
         payload.repeat_days),
    )
    todo_id = cur.lastrowid
    _todo_tags_upsert(db, todo_id, payload.tags)
    db.commit()
    return _todo_to_dict(db.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone(), db)


@app.get("/todos/{todo_id}", dependencies=[Depends(require_auth)])
def get_todo(todo_id: int, db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return _todo_to_dict(row, db)


@app.patch("/todos/{todo_id}", dependencies=[Depends(require_auth)])
def patch_todo(todo_id: int, payload: TodoPatch, db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    raw = payload.model_dump()
    fields = {}
    for k, v in raw.items():
        if k == "tags":
            continue
        if k == "remind_at":
            if v != "__unset__":
                fields[k] = v  # can be None (clears) or a string (sets)
        elif v is not None:
            fields[k] = v
    if fields:
        set_clause = ", ".join(f"{k}=?" for k in fields)
        db.execute(f"UPDATE todos SET {set_clause} WHERE id=?", [*fields.values(), todo_id])
    if payload.tags is not None:
        _todo_tags_upsert(db, todo_id, payload.tags)
    db.commit()
    return _todo_to_dict(db.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone(), db)


@app.post("/todos/{todo_id}/done", dependencies=[Depends(require_auth)])
def todo_done(todo_id: int, payload: TodoActionNotes = TodoActionNotes(), db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    notes_val = payload.notes if payload.notes is not None else dict(row).get("notes", "")
    db.execute(
        "UPDATE todos SET status='done', completed_at=datetime('now'), notes=? WHERE id=?",
        (notes_val, todo_id),
    )
    db.commit()
    return _todo_to_dict(db.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone(), db)


@app.post("/todos/{todo_id}/cancel", dependencies=[Depends(require_auth)])
def todo_cancel(todo_id: int, payload: TodoActionNotes = TodoActionNotes(), db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    notes_val = payload.notes if payload.notes is not None else dict(row).get("notes", "")
    db.execute(
        "UPDATE todos SET status='cancelled', cancelled_at=datetime('now'), notes=? WHERE id=?",
        (notes_val, todo_id),
    )
    db.commit()
    return _todo_to_dict(db.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone(), db)


@app.post("/todos/{todo_id}/start", dependencies=[Depends(require_auth)])
def todo_start(todo_id: int, db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    db.execute(
        "UPDATE todos SET status='in_progress', started_at=COALESCE(started_at, datetime('now')) WHERE id=?",
        (todo_id,),
    )
    db.commit()
    return _todo_to_dict(db.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone(), db)


@app.post("/todos/{todo_id}/defer", dependencies=[Depends(require_auth)])
def todo_defer(todo_id: int, payload: TodoDeferBody, db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    notes_val = payload.notes if payload.notes is not None else dict(row).get("notes", "")
    db.execute(
        "UPDATE todos SET status='pending', due_at=?, has_time=?, remind_at=?, notes=? WHERE id=?",
        (payload.to, payload.has_time, payload.remind_at, notes_val, todo_id),
    )
    db.commit()
    return _todo_to_dict(db.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone(), db)


@app.delete("/todos/{todo_id}", dependencies=[Depends(require_auth)], status_code=204)
def delete_todo(todo_id: int, db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT id FROM todos WHERE id=?", (todo_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    db.execute("DELETE FROM todos WHERE id=?", (todo_id,))
    db.commit()


@app.post("/memories/rechunk", dependencies=[Depends(require_auth)])
def rechunk_memories(
    slug:  Optional[str] = None,
    force: bool          = False,
    db: sqlite3.Connection = Depends(get_db),
):
    """
    Перестроить чанки. Без параметров — только записи без чанков (докатка),
    force=true — вся база заново, slug=... — одна запись.
    """
    if slug:
        rows = db.execute(
            "SELECT id, description, body_md FROM memories WHERE slug = ?", (slug,)
        ).fetchall()
        if not rows:
            raise HTTPException(status_code=404, detail="Not found")
    elif force:
        rows = db.execute("SELECT id, description, body_md FROM memories").fetchall()
    else:
        rows = db.execute(
            """SELECT id, description, body_md FROM memories
               WHERE id NOT IN (SELECT DISTINCT memory_id FROM memory_chunks)"""
        ).fetchall()

    total_chunks = ok = failed = 0
    for r in rows:
        n = _upsert_chunks(db, r["id"], f"{r['description']}\n\n{r['body_md']}")
        if n:
            ok += 1
            total_chunks += n
        else:
            failed += 1
    db.commit()
    return {
        "processed": len(rows), "reindexed": ok, "failed": failed,
        "chunks_created": total_chunks,
        "avg_chunks": round(total_chunks / ok, 1) if ok else 0,
    }


# ── Corpus DB ──────────────────────────────────────────────────────────────────
# Корпус образцов живого письма для проекта project-copywriting-human-voice.
# Ключевое отличие от memories: записи ДОБАВЛЯЮТСЯ, а не перезаписываются —
# накопление идёт месяцами из двух каналов (Telegram-бот и Claude Code).

CORPUS_STATUSES = {"new", "analyzed", "rejected"}

_PLATFORM_HINTS = [
    ("t.me",          "telegram"),
    ("telegram.me",   "telegram"),
    ("vk.com",        "vk"),
    ("vk.ru",         "vk"),
    ("dzen.ru",       "dzen"),
    ("zen.yandex",    "dzen"),
    ("habr.com",      "habr"),
    ("pikabu.ru",     "pikabu"),
    ("livejournal",   "lj"),
    ("teletype.in",   "teletype"),
    ("substack.com",  "substack"),
    ("medium.com",    "medium"),
]


def _detect_platform(url: str) -> str:
    low = url.lower()
    for hint, name in _PLATFORM_HINTS:
        if hint in low:
            return name
    return "blog"


def _init_corpus_db():
    conn = _connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS corpus_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            url         TEXT    NOT NULL UNIQUE,
            platform    TEXT    DEFAULT '',
            title       TEXT    DEFAULT '',
            note        TEXT    DEFAULT '',
            excerpt     TEXT    DEFAULT '',
            source      TEXT    DEFAULT '',
            status      TEXT    DEFAULT 'new'
                        CHECK(status IN ('new','analyzed','rejected')),
            created_at  TEXT    DEFAULT (datetime('now')),
            updated_at  TEXT    DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_corpus_status   ON corpus_items(status);
        CREATE INDEX IF NOT EXISTS idx_corpus_platform ON corpus_items(platform);

        CREATE TABLE IF NOT EXISTS corpus_tags (
            item_id INTEGER NOT NULL REFERENCES corpus_items(id) ON DELETE CASCADE,
            tag     TEXT    NOT NULL,
            PRIMARY KEY (item_id, tag)
        );
        CREATE INDEX IF NOT EXISTS idx_corpus_tags ON corpus_tags(tag);

        CREATE TRIGGER IF NOT EXISTS corpus_updated_at AFTER UPDATE ON corpus_items BEGIN
            UPDATE corpus_items SET updated_at = datetime('now') WHERE id = new.id;
        END;
    """)
    conn.commit()
    conn.close()


def _corpus_tags_upsert(conn: sqlite3.Connection, item_id: int, tags: list[str]):
    conn.execute("DELETE FROM corpus_tags WHERE item_id = ?", (item_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO corpus_tags(item_id, tag) VALUES (?, ?)",
        [(item_id, t.strip().lower()) for t in tags if t.strip()],
    )


def _corpus_to_dict(row: sqlite3.Row, conn: sqlite3.Connection) -> dict:
    d = dict(row)
    tags = conn.execute(
        "SELECT tag FROM corpus_tags WHERE item_id = ? ORDER BY tag", (d["id"],)
    ).fetchall()
    d["tags"] = [t["tag"] for t in tags]
    return d


class CorpusIn(BaseModel):
    url:      str
    title:    str       = ""
    note:     str       = ""
    excerpt:  str       = ""
    platform: str       = ""
    source:   str       = ""
    tags:     list[str] = []


class CorpusPatch(BaseModel):
    title:    Optional[str]       = None
    note:     Optional[str]       = None
    excerpt:  Optional[str]       = None
    platform: Optional[str]       = None
    status:   Optional[str]       = None
    tags:     Optional[list[str]] = None


@app.post("/corpus", dependencies=[Depends(require_auth)], status_code=201)
def corpus_add(payload: CorpusIn, db: sqlite3.Connection = Depends(get_db)):
    url = payload.url.strip()
    if not url:
        raise HTTPException(status_code=422, detail="url is required")

    existing = db.execute("SELECT * FROM corpus_items WHERE url = ?", (url,)).fetchone()
    if existing:
        # Идемпотентно: повторная присылка той же ссылки не плодит дубликат и
        # не затирает уже собранные заметки — только дополняет пустые поля.
        item = _corpus_to_dict(existing, db)
        fields = {}
        for key in ("title", "note", "excerpt"):
            new_val = getattr(payload, key).strip()
            if new_val and not item.get(key):
                fields[key] = new_val
        if fields:
            db.execute(
                f"UPDATE corpus_items SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                [*fields.values(), item["id"]],
            )
        if payload.tags:
            merged = sorted(set(item["tags"]) | {t.strip().lower() for t in payload.tags if t.strip()})
            _corpus_tags_upsert(db, item["id"], merged)
        db.commit()
        result = _corpus_to_dict(
            db.execute("SELECT * FROM corpus_items WHERE id=?", (item["id"],)).fetchone(), db
        )
        result["duplicate"] = True
        return result

    cur = db.execute(
        "INSERT INTO corpus_items(url,platform,title,note,excerpt,source) VALUES(?,?,?,?,?,?)",
        (url, payload.platform.strip() or _detect_platform(url),
         payload.title, payload.note, payload.excerpt, payload.source),
    )
    item_id = cur.lastrowid
    _corpus_tags_upsert(db, item_id, payload.tags)
    db.commit()
    result = _corpus_to_dict(
        db.execute("SELECT * FROM corpus_items WHERE id=?", (item_id,)).fetchone(), db
    )
    result["duplicate"] = False
    return result


@app.get("/corpus", dependencies=[Depends(require_auth)])
def corpus_list(
    status:   Optional[str] = None,
    platform: Optional[str] = None,
    tags:     Optional[str] = None,
    limit:    int           = Query(100, le=1000),
    offset:   int           = 0,
    db: sqlite3.Connection  = Depends(get_db),
):
    conditions, params = [], []
    if status:
        conditions.append("status = ?"); params.append(status)
    if platform:
        conditions.append("platform = ?"); params.append(platform)
    if tags:
        tag_list = [t.strip().lower() for t in tags.split(",") if t.strip()]
        ph = ",".join("?" * len(tag_list))
        conditions.append(
            f"EXISTS (SELECT 1 FROM corpus_tags ct WHERE ct.item_id=corpus_items.id AND ct.tag IN ({ph}))"
        )
        params.extend(tag_list)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.extend([limit, offset])
    rows = db.execute(
        f"SELECT * FROM corpus_items {where} ORDER BY id DESC LIMIT ? OFFSET ?", params
    ).fetchall()
    return [_corpus_to_dict(r, db) for r in rows]


@app.get("/corpus/stats", dependencies=[Depends(require_auth)])
def corpus_stats(db: sqlite3.Connection = Depends(get_db)):
    total = db.execute("SELECT COUNT(*) FROM corpus_items").fetchone()[0]
    by_platform = {
        r["platform"]: r["n"] for r in db.execute(
            "SELECT platform, COUNT(*) n FROM corpus_items GROUP BY platform ORDER BY n DESC"
        )
    }
    by_status = {
        r["status"]: r["n"] for r in db.execute(
            "SELECT status, COUNT(*) n FROM corpus_items GROUP BY status"
        )
    }
    by_source = {
        r["source"]: r["n"] for r in db.execute(
            "SELECT source, COUNT(*) n FROM corpus_items GROUP BY source"
        )
    }
    first = db.execute("SELECT MIN(created_at) FROM corpus_items").fetchone()[0]
    last  = db.execute("SELECT MAX(created_at) FROM corpus_items").fetchone()[0]
    with_note = db.execute("SELECT COUNT(*) FROM corpus_items WHERE note != ''").fetchone()[0]
    return {
        "total": total, "with_note": with_note,
        "by_platform": by_platform, "by_status": by_status, "by_source": by_source,
        "first_added": first, "last_added": last,
    }


@app.get("/corpus/{item_id}", dependencies=[Depends(require_auth)])
def corpus_get(item_id: int, db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT * FROM corpus_items WHERE id=?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return _corpus_to_dict(row, db)


@app.patch("/corpus/{item_id}", dependencies=[Depends(require_auth)])
def corpus_patch(item_id: int, payload: CorpusPatch, db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT * FROM corpus_items WHERE id=?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    if payload.status is not None and payload.status not in CORPUS_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {sorted(CORPUS_STATUSES)}")
    fields = {k: v for k, v in payload.model_dump().items() if k != "tags" and v is not None}
    if fields:
        db.execute(
            f"UPDATE corpus_items SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
            [*fields.values(), item_id],
        )
    if payload.tags is not None:
        _corpus_tags_upsert(db, item_id, payload.tags)
    db.commit()
    return _corpus_to_dict(db.execute("SELECT * FROM corpus_items WHERE id=?", (item_id,)).fetchone(), db)


@app.delete("/corpus/{item_id}", dependencies=[Depends(require_auth)], status_code=204)
def corpus_delete(item_id: int, db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT id FROM corpus_items WHERE id=?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    db.execute("DELETE FROM corpus_items WHERE id=?", (item_id,))
    db.commit()


# ── Memories embed-all ─────────────────────────────────────────────────────────

@app.post("/memories/embed-all", dependencies=[Depends(require_auth)])
def embed_all(db: sqlite3.Connection = Depends(get_db)):
    """Batch generate embeddings for all records missing them."""
    existing_vecs = {r[0] for r in db.execute("SELECT rowid FROM vec_memories").fetchall()}
    rows = db.execute("SELECT id, description, body_md FROM memories").fetchall()
    missing = [r for r in rows if r["id"] not in existing_vecs]

    if not missing:
        return {"message": "All records already embedded", "count": 0}

    ok = fail = 0
    for r in missing:
        text = f"{r['description']}\n\n{r['body_md']}"
        try:
            vec = embed_text(text)
            db.execute(
                "INSERT OR REPLACE INTO vec_memories(rowid, embedding) VALUES (?,?)",
                (r["id"], vec),
            )
            ok += 1
        except Exception as e:
            logger.warning(f"Embed failed id={r['id']}: {e}")
            fail += 1

    db.commit()
    return {"embedded": ok, "failed": fail, "total": len(rows)}
