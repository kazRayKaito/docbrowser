from typing import Optional
from db import search as db_search


def build_snippet(full_text: str, query: str, length: int) -> str:
    if not full_text:
        return ""
    lower_text = full_text.lower()
    # Try to find the first query term in the text
    first_term = query.strip().split()[0].lower() if query.strip() else ""
    pos = lower_text.find(first_term) if first_term else -1
    if pos == -1:
        pos = 0
    start = max(0, pos - length // 3)
    end = min(len(full_text), start + length)
    snippet = full_text[start:end]
    if start > 0:
        snippet = "..." + snippet
    if end < len(full_text):
        snippet = snippet + "..."
    return snippet


async def run_search(
    query: str,
    ext_filter: Optional[str],
    page: int,
    per_page: int,
    db_path: str,
) -> tuple[list, int]:
    rows, total = await db_search(query, ext_filter, page, per_page, db_path)
    results = []
    for row in rows:
        snippet = build_snippet(row.get("full_text", ""), query, 200)
        results.append({
            "id": row["id"],
            "filename": row["filename"],
            "extension": row["extension"],
            "ocr_engine": row["ocr_engine"],
            "smb_uri": row["smb_uri"],
            "smb_parent": row["smb_parent"],
            "snippet": snippet,
            "indexed_at": row["indexed_at"],
        })
    return results, total
