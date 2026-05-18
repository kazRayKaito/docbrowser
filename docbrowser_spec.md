# DocBrowser — Personal Document Search System
## Full Specification

---

## Overview

A self-hosted document indexing and search tool. A background scanner walks an SMB-mounted
directory on a daily schedule, extracts text from PDFs and images (using OCR where needed),
stores results in SQLite, and exposes a minimal web UI for search and scan management.

**Design priorities: functionality over aesthetics. Simple stack, easy to maintain.**

---

## Project Directory Structure

```
docbrowser/
├── docker-compose.yml
├── config.yaml              ← user-editable, mounted into container as read-only
├── app/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py              ← FastAPI app entry point
│   ├── scanner.py           ← file walker + OCR pipeline
│   ├── db.py                ← SQLite operations (registry + FTS5)
│   ├── search.py            ← FTS5 query logic + snippet extraction
│   └── templates/
│       ├── index.html       ← search UI
│       └── scan.html        ← scan management UI
└── data/
    ├── db/                  ← SQLite files (Docker named volume)
    └── docs/                ← bind mount from host SMB mount point
```

---

## config.yaml

This is the only file the user needs to edit for configuration.
It is mounted into the container as read-only at `/app/config.yaml`.

```yaml
smb:
  host: 192.168.0.212
  share: d
  mount_point: /data/docs    # path as seen inside the Docker container

scan:
  schedule: "02:00"          # daily scan time in local time (HH:MM, 24h)
  whitelist_extensions:
    - pdf
    - jpg
    - jpeg
    - png
    - tiff
    - tif
    - gif
    - bmp
    - webp
  blacklist_patterns:
    - "*/site-packages/*"
    - "*/node_modules/*"
    - "*/.git/*"
    - "*/venv/*"
    - "*/.venv/*"
    - "*/__pycache__/*"
    - "*/dist-packages/*"
    - "*/build/*"
    - "*/.DS_Store"

ocr:
  engine: paddleocr
  language: japan            # PaddleOCR language code for Japanese
  fallback_char_threshold: 50  # if extracted char count < this, treat PDF page as scanned

search:
  results_per_page: 20
  snippet_length: 200        # characters of context shown around a match
```

---

## Scale Assumptions

- ~5,500 target files (after filtering out site-packages, node_modules, etc.)
- Total data: ~600GB on SMB share, but most is excluded by blacklist
- File types: mixed PDFs (native text and scanned), JPG/PNG/TIFF images
- Language: mixed Japanese and other languages
- First full scan estimate: ~15 hours (CPU OCR, single worker)
- System must remain searchable during indexing (partial index is acceptable)

---

## SQLite Schema

Single database file at `/data/db/registry.db`.
Two tables: file registry and FTS5 full-text index.

```sql
-- File registry: one row per discovered file
CREATE TABLE IF NOT EXISTS files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    filepath      TEXT UNIQUE NOT NULL,   -- absolute path inside container (/data/docs/...)
    smb_uri       TEXT NOT NULL,          -- smb://192.168.0.212/d/relative/path/file.pdf
    smb_parent    TEXT NOT NULL,          -- smb://192.168.0.212/d/relative/path/
    filename      TEXT NOT NULL,
    extension     TEXT NOT NULL,
    file_size     INTEGER,
    file_mtime    REAL NOT NULL,          -- unix timestamp; used for change detection
    indexed_at    REAL,                   -- unix timestamp of last successful index
    ocr_status    TEXT DEFAULT 'pending', -- pending | done | failed | skipped
    ocr_engine    TEXT,                   -- 'pymupdf' | 'paddleocr' | 'skipped'
    page_count    INTEGER,
    error_msg     TEXT                    -- populated on ocr_status = 'failed'
);

CREATE INDEX IF NOT EXISTS idx_status    ON files(ocr_status);
CREATE INDEX IF NOT EXISTS idx_extension ON files(extension);

-- FTS5 full-text index: one row per file (document-level granularity)
CREATE VIRTUAL TABLE IF NOT EXISTS fts
USING fts5(
    file_id UNINDEXED,   -- INTEGER foreign key to files.id (not searchable)
    filename,            -- searchable: original filename
    full_text,           -- searchable: all extracted/OCR'd text
    content='',          -- contentless mode: text stored here only, not duplicated
    tokenize='unicode61' -- handles CJK character-level matching adequately
);
```

### Notes on unicode61 tokenizer

FTS5's `unicode61` tokenizer does not perform Japanese morphological analysis (no MeCab/Sudachi).
It tokenizes on Unicode boundaries and allows substring matching. For personal search use,
typing `領収書` or `会議録` will find matches correctly. Ranking quality is lower than
Meilisearch but sufficient for this scale.

If Japanese search quality needs improvement later, replace SQLite FTS5 with
Meilisearch as a second Docker service.

---

## Scanner Pipeline

### Trigger conditions
- Daily at time specified in `config.yaml` (APScheduler, AsyncIOScheduler)
- Manual trigger via POST `/api/scan/start` from the scan UI

### File discovery logic

```
WALK /data/docs recursively (os.walk or pathlib.Path.rglob)
  └── for each file:
        1. check file extension against whitelist  → skip if not in list
        2. check full path against blacklist patterns (fnmatch)  → skip if matched
        3. query files table WHERE filepath = ? AND file_mtime = ?
             → record exists with same mtime: skip (already indexed)
             → record missing or mtime changed: proceed to classify
```

### File classification

```
CLASSIFY
  └── extension in image list (jpg, jpeg, png, tiff, tif, gif, bmp, webp)?
        → route to OCR path

      extension is pdf?
        → open with pymupdf (fitz)
        → extract text from first 2 pages
        → if total char count < ocr.fallback_char_threshold (default 50)
             → route to OCR path (scanned PDF)
        → else
             → route to pymupdf path (native text PDF)

      anything else → skip, record as ocr_status='skipped'
```

### Text extraction

```
EXTRACT (pymupdf path — native text PDF)
  └── open with fitz.open()
      extract text from all pages: page.get_text()
      join with newlines
      record ocr_engine = 'pymupdf'

EXTRACT (OCR path — scanned PDF)
  └── open with fitz.open()
      for each page:
        render to image: page.get_pixmap(dpi=150)
        convert pixmap to numpy array
        run PaddleOCR on array
        collect text lines
      join all pages with newlines
      record ocr_engine = 'paddleocr'

EXTRACT (OCR path — image file)
  └── open image with Pillow
      convert to numpy array (RGB)
      run PaddleOCR on array
      join text lines
      record ocr_engine = 'paddleocr'
```

### Storage

```
STORE
  └── if file previously indexed (mtime changed):
        DELETE FROM fts WHERE file_id = <old id>
        UPDATE files SET ... WHERE filepath = ?

      INSERT OR REPLACE INTO files (all metadata, ocr_status='done')
      INSERT INTO fts (file_id, filename, full_text)

      on any exception during extraction:
        INSERT OR REPLACE INTO files (..., ocr_status='failed', error_msg=str(e))
        do NOT write to fts
```

### Progress tracking (in-memory, reset on restart)

The scanner maintains a simple dict in memory that the SSE endpoint reads:

```python
scan_state = {
    "running": False,
    "paused": False,
    "total_discovered": 0,
    "done": 0,
    "failed": 0,
    "skipped": 0,
    "current_file": "",
    "started_at": None,
    "eta_seconds": None,
}
```

---

## SMB URI Construction

The host mounts the SMB share before starting Docker.
Docker sees it as a read-only bind mount at `/data/docs`.

```python
def to_smb_uri(filepath: str, config: dict) -> str:
    """
    filepath:    /data/docs/archive/2023/receipt.pdf  (absolute, inside container)
    mount_point: /data/docs
    host:        192.168.0.212
    share:       d

    returns:     smb://192.168.0.212/d/archive/2023/receipt.pdf
    """
    relative = filepath.removeprefix(config["smb"]["mount_point"])
    return f"smb://{config['smb']['host']}/{config['smb']['share']}{relative}"

def to_smb_parent(smb_uri: str) -> str:
    """
    smb://192.168.0.212/d/archive/2023/receipt.pdf
    → smb://192.168.0.212/d/archive/2023/
    """
    return smb_uri.rsplit("/", 1)[0] + "/"
```

macOS handles `smb://` URIs natively when clicked as HTML links.
The share must already be mounted on the Mac host for the link to work.

---

## API Endpoints

All endpoints served by FastAPI on port 8000.

### Pages (HTML)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Search UI (index.html) |
| GET | `/scan` | Scan management UI (scan.html) |

### API (JSON)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/scan/start` | Trigger immediate scan (idempotent if already running) |
| POST | `/api/scan/pause` | Pause a running scan |
| POST | `/api/scan/resume` | Resume a paused scan |
| GET | `/api/scan/status` | SSE stream of scan_state dict (updates every 2s) |
| GET | `/api/search` | Full-text search (see params below) |
| GET | `/health` | `{"status": "ok"}` |

### GET /api/search parameters

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `q` | string | required | Search query |
| `ext` | string | (all) | Filter by extension: pdf, jpg, png, etc. |
| `page` | int | 1 | Page number (1-indexed) |

### Search response JSON

```json
{
  "query": "領収書",
  "total": 84,
  "page": 1,
  "pages": 5,
  "results": [
    {
      "id": 42,
      "filename": "receipt_2023.pdf",
      "extension": "pdf",
      "ocr_engine": "paddleocr",
      "smb_uri": "smb://192.168.0.212/d/docs/receipt_2023.pdf",
      "smb_parent": "smb://192.168.0.212/d/docs/",
      "snippet": "...領収書　合計金額 ¥12,500 品目: 事務用品...",
      "indexed_at": "2026-05-17T02:14:33"
    }
  ]
}
```

### SSE scan/status event shape

```json
{
  "running": true,
  "paused": false,
  "total_discovered": 5500,
  "done": 1240,
  "failed": 3,
  "skipped": 890,
  "current_file": "/data/docs/archive/2021/scan_0042.pdf",
  "started_at": "2026-05-17T02:00:00",
  "eta_seconds": 38400
}
```

---

## UI: Search Page (index.html)

**Functionality required:**
- Search input box, submits on Enter or button click
- Extension filter dropdown: All / PDF / JPG / PNG / TIFF / Other
- Results list, each item shows:
  - Filename (bold)
  - Extension badge (small label)
  - OCR engine used (pymupdf or paddleocr, small text)
  - Snippet with matched text (show as-is, no highlighting required at v1)
  - "Open File" button → `<a href="{smb_uri}">Open File</a>`
  - "Open Folder" button → `<a href="{smb_parent}">Open Folder</a>`
  - Indexed date (small text)
- Pagination: Previous / Next / page X of Y
- Result count: "84 results for 領収書"
- No results state: "No results found."

**Implementation:** Plain HTML, vanilla JS fetch to `/api/search`. No framework, no build step.

---

## UI: Scan Management Page (scan.html)

**Functionality required:**
- Status panel showing scan_state fields:
  - Progress bar: done / total_discovered
  - Counts: Done / Failed / Skipped
  - Current file being processed (truncated path)
  - ETA (formatted as hours and minutes)
  - Running / Paused / Idle status indicator
- Controls:
  - "Start Scan" button (POST /api/scan/start)
  - "Pause" button (POST /api/scan/pause)
  - "Resume" button (POST /api/scan/resume)
- Error log table (last 50 failed files):
  - Columns: filepath, error message
  - Populated by querying `SELECT filepath, error_msg FROM files WHERE ocr_status='failed'`
- Page auto-refreshes scan_state via SSE (EventSource to /api/scan/status)

**Implementation:** Plain HTML, vanilla JS SSE + fetch. No framework.

---

## Docker Setup

### docker-compose.yml

```yaml
services:
  app:
    build: ./app
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - ./config.yaml:/app/config.yaml:ro
      - db_data:/data/db
      - /Volumes/d:/data/docs:ro    # host SMB mount point → container
    environment:
      - TZ=Asia/Tokyo

volumes:
  db_data:
```

### app/Dockerfile

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# System dependencies for PaddleOCR and pymupdf
RUN apt-get update && apt-get install -y \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# PaddleOCR downloads models on first run; pre-download at build time
RUN python -c "from paddleocr import PaddleOCR; PaddleOCR(use_angle_cls=True, lang='japan')"

COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### app/requirements.txt

```
fastapi
uvicorn[standard]
paddleocr
paddlepaddle
pymupdf
pillow
pyyaml
apscheduler
sse-starlette
aiosqlite
numpy
```

---

## SMB Mount on Mac Host

The Docker container cannot mount SMB shares itself.
The Mac host must mount the share before `docker compose up`.

**Manual mount (for testing):**
```bash
open smb://192.168.0.212/d
# share appears at /Volumes/d
```

**Automatic mount at login:**
Go to System Settings → General → Login Items → Add the mounted volume,
or add the SMB URL to "Open at Login" items.

Verify the mount point matches the bind mount in docker-compose.yml:
`/Volumes/d:/data/docs:ro`

If the share mounts to a different path (e.g. `/Volumes/d - 1`), update docker-compose.yml accordingly.

---

## Python Module Responsibilities

### db.py
- `init_db(db_path)` — create tables and indexes if not exist
- `get_file_record(filepath)` → row or None
- `upsert_file(metadata_dict)` — INSERT OR REPLACE into files
- `upsert_fts(file_id, filename, full_text)` — INSERT into fts
- `delete_fts(file_id)` — DELETE old FTS entry before re-index
- `get_failed_files(limit=50)` → list of (filepath, error_msg)
- `search(query, ext_filter, page, per_page)` → (results list, total count)

### scanner.py
- `load_config(path)` → parsed config dict
- `is_whitelisted(filepath, config)` → bool
- `is_blacklisted(filepath, config)` → bool (fnmatch against patterns)
- `classify_file(filepath)` → 'pymupdf' | 'paddleocr' | 'skip'
- `extract_pymupdf(filepath)` → (text, page_count)
- `extract_paddleocr_pdf(filepath, ocr_instance)` → (text, page_count)
- `extract_paddleocr_image(filepath, ocr_instance)` → (text, page_count=1)
- `run_scan(config, db_path, scan_state)` — main scan loop, updates scan_state in place

### search.py
- `build_snippet(full_text, query, length)` → str
- `run_search(query, ext_filter, page, per_page, db_path)` → (results, total)

### main.py
- FastAPI app setup
- Lifespan: init DB, start APScheduler with daily scan job
- Mount Jinja2 templates
- Define all routes (pages + API endpoints)
- SSE endpoint reads scan_state dict

---

## Build Order (recommended)

Build in this order so the system is partially usable before OCR is integrated.

1. **db.py** — schema, insert helpers, search query
2. **scanner.py** — file walker with whitelist/blacklist, pymupdf extraction only (no OCR yet)
3. **Validate** — run scanner against actual share, check what is discovered vs filtered
4. **search.py** — FTS5 query with snippet extraction
5. **main.py** — FastAPI, search endpoint, scan trigger, SSE status
6. **index.html + scan.html** — UI, plain HTML + vanilla JS
7. **End-to-end test** — system is usable for native PDFs at this point
8. **Add PaddleOCR** to scanner.py — OCR path for scanned PDFs and images
9. **Dockerfile + docker-compose.yml** — containerize
10. **APScheduler daily job** — wire into FastAPI lifespan startup
11. **Test on Docker** — verify SMB bind mount, volume persistence, timezone

---

## Known Constraints and Notes

- **MPS/GPU**: Docker Desktop on macOS does not expose Apple Silicon GPU to containers.
  PaddleOCR runs on CPU inside Docker. At 5,500 files this is acceptable (~15h first scan).
  If scan time becomes a problem, consider running scanner.py natively on macOS with a
  venv and pointing it at the same SQLite DB and Meilisearch instance.

- **SMB mount dependency**: If the Mac host reboots and the SMB share is not remounted,
  the container starts but the `/data/docs` directory appears empty. The scanner will
  discover 0 files and do nothing (safe, no data loss). Add a health check or startup
  script to verify the mount exists before scanning.

- **PaddleOCR first-run model download**: The Dockerfile pre-downloads OCR models at
  build time to avoid download delays at runtime. Image will be ~3–4GB.

- **Contentless FTS5**: The `content=''` option means FTS5 stores text independently.
  If a file is re-indexed, the old FTS row must be deleted manually before inserting
  the new one (handled in `db.upsert_fts` via delete then insert).

- **unicode61 tokenizer limitation**: Japanese search works by substring matching, not
  morphological analysis. Searching for a stem may not find inflected forms. This is
  acceptable for document search (searching exact terms from documents).

- **Large image files**: Very large TIFFs (scanned at high DPI) may cause memory spikes
  during OCR. Consider resizing to max 2400px on the longest side before passing to
  PaddleOCR if OOM errors appear.
