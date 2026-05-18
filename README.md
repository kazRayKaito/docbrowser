# DocBrowser

Self-hosted document indexing and search for SMB-mounted file shares.
Extracts text from PDFs (native + scanned) and images using PaddleOCR,
stores results in SQLite FTS5, and provides a minimal web UI.

## Requirements

- Docker Desktop on macOS
- SMB share mounted on the host at `/Volumes/d` (or update `docker-compose.yml`)

## Quick Start

1. Mount your SMB share:
   ```bash
   open smb://192.168.0.212/d
   # Verify it appears at /Volumes/d
   ```

2. Edit `config.yaml` — set your SMB host/share and scan schedule.

3. Build and start:
   ```bash
   docker compose up --build
   ```
   First build downloads PaddleOCR models (~3–4 GB image).

4. Open `http://localhost:8000` — search UI.
   Open `http://localhost:8000/scan` — trigger and monitor scans.

## Notes

- PaddleOCR runs on **CPU** inside Docker (no GPU access on macOS).
  First full scan of ~5,500 files takes ~15 hours.
- The SQLite DB persists in the `db_data` Docker named volume.
- Timezone defaults to `Asia/Tokyo` — change `TZ` in `docker-compose.yml`.
- `smb://` links in search results open natively on macOS if the share is mounted.
