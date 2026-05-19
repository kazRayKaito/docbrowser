import asyncio
import fnmatch
import logging
import os
import time
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger("uvicorn")
_ocr_instance = None


def load_config(path: str = "/app/config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def to_smb_uri(filepath: str, config: dict) -> str:
    mount_point = config["smb"]["mount_point"]
    relative = filepath.removeprefix(mount_point)
    return f"smb://{config['smb']['host']}/{config['smb']['share']}{relative}"


def to_smb_parent(smb_uri: str) -> str:
    return smb_uri.rsplit("/", 1)[0] + "/"


def is_whitelisted(filepath: str, config: dict) -> bool:
    ext = Path(filepath).suffix.lstrip(".").lower()
    return ext in config["scan"]["whitelist_extensions"]


def is_blacklisted(filepath: str, config: dict) -> bool:
    for pattern in config["scan"]["blacklist_patterns"]:
        if fnmatch.fnmatch(filepath, pattern):
            return True
    return False


def classify_file(filepath: str, config: dict) -> str:
    ext = Path(filepath).suffix.lstrip(".").lower()
    image_exts = {"jpg", "jpeg", "png", "tiff", "tif", "gif", "bmp", "webp"}
    if ext in image_exts:
        return "ocr"
    if ext == "pdf":
        try:
            import fitz
            doc = fitz.open(filepath)
            sample_text = ""
            for i in range(min(2, len(doc))):
                sample_text += doc[i].get_text()
            doc.close()
            threshold = config["ocr"].get("fallback_char_threshold", 50)
            if len(sample_text.strip()) < threshold:
                return "ocr"
            return "pymupdf"
        except Exception:
            return "ocr"
    return "skip"


def _get_ocr(config: dict):
    global _ocr_instance
    if _ocr_instance is None:
        import pytesseract
        _ocr_instance = pytesseract
    return _ocr_instance


def extract_pymupdf(filepath: str) -> tuple[str, int]:
    import fitz
    doc = fitz.open(filepath)
    pages = [doc[i].get_text() for i in range(len(doc))]
    doc.close()
    return "\n".join(pages), len(pages)


MAX_SIDE = 4000
# --oem 1: LSTM neural net only (most accurate)
# --psm 6: assume a uniform block of text (best for full scanned pages)
TESS_CONFIG = "--oem 1 --psm 6"


def _preprocess(img):
    from PIL import ImageOps
    img = img.convert("L")
    img = ImageOps.autocontrast(img)
    return img


def _resize_if_needed(img):
    from PIL import Image
    w, h = img.size
    longest = max(w, h)
    if longest <= MAX_SIDE:
        return img
    scale = MAX_SIDE / longest
    return img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)


def extract_ocr_pdf(filepath: str, ocr) -> tuple[str, int]:
    import fitz
    from PIL import Image
    doc = fitz.open(filepath)
    texts = []
    for i in range(len(doc)):
        pix = doc[i].get_pixmap(dpi=250)
        img = Image.frombytes("RGB", [pix.w, pix.h], pix.samples)
        img = _preprocess(_resize_if_needed(img))
        text = ocr.image_to_string(img, lang="jpn+jpn_vert", config=TESS_CONFIG)
        if text.strip():
            texts.append(text)
    page_count = len(doc)
    doc.close()
    return "\n".join(texts), page_count


def extract_ocr_image(filepath: str, ocr) -> tuple[str, int]:
    from PIL import Image
    img = Image.open(filepath).convert("RGB")
    img = _preprocess(_resize_if_needed(img))
    text = ocr.image_to_string(img, lang="jpn+jpn_vert", config=TESS_CONFIG)
    return text, 1


async def run_scan(config: dict, db_path: str, scan_state: dict):
    from db import get_file_record, upsert_file, upsert_fts

    scan_state.update({
        "running": True,
        "paused": False,
        "phase": "discovering",
        "total_discovered": 0,
        "done": 0,
        "failed": 0,
        "skipped": 0,
        "current_file": "",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "eta_seconds": None,
    })

    mount_point = config["smb"]["mount_point"]
    candidates = []
    for root, dirs, files in os.walk(mount_point):
        dirs[:] = [
            d for d in dirs
            if not is_blacklisted(os.path.join(root, d, "_"), config)
        ]
        scan_state["current_file"] = root
        for fname in files:
            fpath = os.path.join(root, fname)
            if not is_whitelisted(fpath, config):
                continue
            if is_blacklisted(fpath, config):
                continue
            candidates.append(fpath)
        scan_state["total_discovered"] = len(candidates)
        log.info("Discovering: %s (%d files so far)", root, len(candidates))
        await asyncio.sleep(0)

    scan_state["phase"] = "scanning"
    scan_state["current_file"] = ""
    log.info("Discovery done: %d files to process", len(candidates))

    ocr = None
    start_time = time.monotonic()
    processed = 0

    for fpath in candidates:
        while scan_state.get("paused"):
            await asyncio.sleep(1)

        if not scan_state.get("running"):
            break

        scan_state["current_file"] = fpath

        try:
            stat = os.stat(fpath)
            mtime = stat.st_mtime
            size = stat.st_size

            existing = await get_file_record(fpath, db_path)
            if existing and existing["file_mtime"] == mtime and existing["ocr_status"] == "done":
                scan_state["skipped"] += 1
                processed += 1
                continue

            method = classify_file(fpath, config)

            if method == "skip":
                meta = _build_meta(fpath, config, mtime, size, "skipped", "skipped", None, None)
                await upsert_file(meta, db_path)
                scan_state["skipped"] += 1
                processed += 1
                continue

            if method == "pymupdf":
                log.info("[pymupdf] %s | %s", Path(fpath).name, fpath)
                text, page_count = await asyncio.get_event_loop().run_in_executor(
                    None, extract_pymupdf, fpath
                )
                engine = "pymupdf"
            elif method == "ocr":
                if ocr is None:
                    ocr = _get_ocr(config)
                ext = Path(fpath).suffix.lstrip(".").lower()
                if ext == "pdf":
                    log.info("[ocr/pdf] %s | %s", Path(fpath).name, fpath)
                    text, page_count = await asyncio.get_event_loop().run_in_executor(
                        None, extract_ocr_pdf, fpath, ocr
                    )
                else:
                    log.info("[ocr/image] %s | %s", Path(fpath).name, fpath)
                    text, page_count = await asyncio.get_event_loop().run_in_executor(
                        None, extract_ocr_image, fpath, ocr
                    )
                engine = "tesseract"

            meta = _build_meta(fpath, config, mtime, size, "done", engine, page_count, None)
            file_id = await upsert_file(meta, db_path)
            await upsert_fts(file_id, Path(fpath).name, text, db_path)
            log.info("[done] %s | pages=%s", Path(fpath).name, page_count)
            scan_state["done"] += 1

        except Exception as e:
            log.error("[failed] %s | %s | error: %s", Path(fpath).name, fpath, e, exc_info=True)
            meta = _build_meta(fpath, config, mtime, size, "failed", None, None, str(e))
            await upsert_file(meta, db_path)
            scan_state["failed"] += 1

        processed += 1
        elapsed = time.monotonic() - start_time
        if processed > 0:
            rate = elapsed / processed
            remaining = len(candidates) - processed
            scan_state["eta_seconds"] = int(rate * remaining)

        await asyncio.sleep(0)  # yield to event loop

    scan_state["running"] = False
    scan_state["phase"] = "idle"
    scan_state["current_file"] = ""
    scan_state["eta_seconds"] = None
    log.info(
        "Scan finished: done=%d failed=%d skipped=%d",
        scan_state["done"], scan_state["failed"], scan_state["skipped"],
    )


def _build_meta(fpath, config, mtime, size, status, engine, page_count, error_msg):
    smb_uri = to_smb_uri(fpath, config)
    return {
        "filepath": fpath,
        "smb_uri": smb_uri,
        "smb_parent": to_smb_parent(smb_uri),
        "filename": Path(fpath).name,
        "extension": Path(fpath).suffix.lstrip(".").lower(),
        "file_size": size,
        "file_mtime": mtime,
        "indexed_at": time.time(),
        "ocr_status": status,
        "ocr_engine": engine,
        "page_count": page_count,
        "error_msg": error_msg,
    }
