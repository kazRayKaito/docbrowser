import asyncio
import fnmatch
import os
import time
from pathlib import Path
from typing import Optional

import yaml

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
        return "paddleocr"
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
                return "paddleocr"
            return "pymupdf"
        except Exception:
            return "paddleocr"
    return "skip"


def _get_ocr(config: dict):
    global _ocr_instance
    if _ocr_instance is None:
        import easyocr
        _ocr_instance = easyocr.Reader(['ja', 'en'], gpu=False)
    return _ocr_instance


def extract_pymupdf(filepath: str) -> tuple[str, int]:
    import fitz
    doc = fitz.open(filepath)
    pages = [doc[i].get_text() for i in range(len(doc))]
    doc.close()
    return "\n".join(pages), len(pages)


def _parse_ocr_result(result) -> list[str]:
    lines = []
    for res in result:
        if res is None:
            continue
        # EasyOCR returns list of (bbox, text, confidence)
        if isinstance(res, tuple) and len(res) == 3:
            _, text, _ = res
            if text:
                lines.append(text)
    return lines


MAX_SIDE = 2400


def _resize_if_needed(img):
    import numpy as np
    from PIL import Image
    h, w = img.shape[:2]
    longest = max(h, w)
    if longest <= MAX_SIDE:
        return img
    scale = MAX_SIDE / longest
    new_w, new_h = int(w * scale), int(h * scale)
    pil = Image.fromarray(img).resize((new_w, new_h), Image.LANCZOS)
    return np.array(pil)


def extract_paddleocr_pdf(filepath: str, ocr) -> tuple[str, int]:
    import fitz
    import numpy as np
    doc = fitz.open(filepath)
    texts = []
    for i in range(len(doc)):
        pix = doc[i].get_pixmap(dpi=72)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
        if pix.n == 4:
            img = img[:, :, :3]
        img = _resize_if_needed(img)
        result = ocr.readtext(img)
        lines = _parse_ocr_result(result)
        if lines:
            texts.append("\n".join(lines))
    page_count = len(doc)
    doc.close()
    return "\n".join(texts), page_count


def extract_paddleocr_image(filepath: str, ocr) -> tuple[str, int]:
    import numpy as np
    from PIL import Image
    img = Image.open(filepath).convert("RGB")
    arr = np.array(img)
    arr = _resize_if_needed(arr)
    result = ocr.readtext(arr)
    lines = _parse_ocr_result(result)
    return "\n".join(lines), 1


async def run_scan(config: dict, db_path: str, scan_state: dict):
    from db import get_file_record, upsert_file, upsert_fts

    scan_state.update({
        "running": True,
        "paused": False,
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
        for fname in files:
            fpath = os.path.join(root, fname)
            if not is_whitelisted(fpath, config):
                continue
            if is_blacklisted(fpath, config):
                continue
            candidates.append(fpath)

    scan_state["total_discovered"] = len(candidates)

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
                text, page_count = await asyncio.get_event_loop().run_in_executor(
                    None, extract_pymupdf, fpath
                )
                engine = "pymupdf"
            else:
                if ocr is None:
                    ocr = _get_ocr(config)
                ext = Path(fpath).suffix.lstrip(".").lower()
                if ext == "pdf":
                    text, page_count = await asyncio.get_event_loop().run_in_executor(
                        None, extract_paddleocr_pdf, fpath, ocr
                    )
                else:
                    text, page_count = await asyncio.get_event_loop().run_in_executor(
                        None, extract_paddleocr_image, fpath, ocr
                    )
                engine = "paddleocr"

            meta = _build_meta(fpath, config, mtime, size, "done", engine, page_count, None)
            file_id = await upsert_file(meta, db_path)
            await upsert_fts(file_id, Path(fpath).name, text, db_path)
            scan_state["done"] += 1

        except Exception as e:
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
    scan_state["current_file"] = ""
    scan_state["eta_seconds"] = None


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
