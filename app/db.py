import aiosqlite
import time
from typing import Optional

DB_PATH = "/data/db/registry.db"

CREATE_FILES = """
CREATE TABLE IF NOT EXISTS files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    filepath      TEXT UNIQUE NOT NULL,
    smb_uri       TEXT NOT NULL,
    smb_parent    TEXT NOT NULL,
    filename      TEXT NOT NULL,
    extension     TEXT NOT NULL,
    file_size     INTEGER,
    file_mtime    REAL NOT NULL,
    indexed_at    REAL,
    ocr_status    TEXT DEFAULT 'pending',
    ocr_engine    TEXT,
    page_count    INTEGER,
    error_msg     TEXT
);
"""

CREATE_IDX_STATUS = "CREATE INDEX IF NOT EXISTS idx_status ON files(ocr_status);"
CREATE_IDX_EXT = "CREATE INDEX IF NOT EXISTS idx_extension ON files(extension);"

CREATE_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS fts
USING fts5(
    file_id UNINDEXED,
    filename,
    full_text,
    tokenize='unicode61'
);
"""


async def init_db(db_path: str = DB_PATH):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(CREATE_FILES)
        await db.execute(CREATE_IDX_STATUS)
        await db.execute(CREATE_IDX_EXT)
        await db.execute(CREATE_FTS)
        await db.commit()


async def get_file_record(filepath: str, db_path: str = DB_PATH) -> Optional[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM files WHERE filepath = ?", (filepath,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def upsert_file(meta: dict, db_path: str = DB_PATH) -> int:
    cols = ", ".join(meta.keys())
    placeholders = ", ".join("?" * len(meta))
    vals = list(meta.values())
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            f"INSERT OR REPLACE INTO files ({cols}) VALUES ({placeholders})", vals
        )
        await db.commit()
        async with db.execute("SELECT id FROM files WHERE filepath = ?", (meta["filepath"],)) as cur:
            row = await cur.fetchone()
            return row[0]


async def upsert_fts(file_id: int, filename: str, full_text: str, db_path: str = DB_PATH):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM fts WHERE file_id = ?", (file_id,))
        await db.execute(
            "INSERT INTO fts (file_id, filename, full_text) VALUES (?, ?, ?)",
            (file_id, filename, full_text),
        )
        await db.commit()


async def delete_fts(file_id: int, db_path: str = DB_PATH):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM fts WHERE file_id = ?", (file_id,))
        await db.commit()


async def get_failed_files(limit: int = 50, db_path: str = DB_PATH) -> list:
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT filepath, error_msg FROM files WHERE ocr_status='failed' LIMIT ?", (limit,)
        ) as cur:
            return await cur.fetchall()


def _fts_query(query: str) -> str:
    # Append * to each term for prefix matching (needed for Japanese — no spaces between chars)
    terms = query.strip().split()
    return " ".join(t + "*" if not t.endswith("*") else t for t in terms) if terms else query


async def search(
    query: str,
    ext_filter: Optional[str],
    page: int,
    per_page: int,
    db_path: str = DB_PATH,
) -> tuple[list, int]:
    offset = (page - 1) * per_page
    fts_q = _fts_query(query)

    ext_clause = "AND f.extension = ?" if ext_filter else ""
    params_count = [fts_q]
    params_rows = [fts_q]
    if ext_filter:
        params_count.append(ext_filter)
        params_rows.append(ext_filter)

    count_sql = f"""
        SELECT COUNT(*)
        FROM fts
        JOIN files f ON fts.file_id = f.id
        WHERE fts MATCH ?
        {ext_clause}
    """
    rows_sql = f"""
        SELECT f.id, f.filename, f.extension, f.ocr_engine,
               f.smb_uri, f.smb_parent, f.indexed_at,
               fts.full_text
        FROM fts
        JOIN files f ON fts.file_id = f.id
        WHERE fts MATCH ?
        {ext_clause}
        ORDER BY rank
        LIMIT ? OFFSET ?
    """

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(count_sql, params_count) as cur:
            total = (await cur.fetchone())[0]
        params_rows += [per_page, offset]
        async with db.execute(rows_sql, params_rows) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows], total
