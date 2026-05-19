import asyncio
import json
import math
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional


from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from sse_starlette.sse import EventSourceResponse

from db import init_db, get_failed_files, DB_PATH
from scanner import load_config, run_scan
from search import run_search

CONFIG_PATH = "/app/config.yaml"

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

_scan_task: Optional[asyncio.Task] = None
config: dict = {}
scheduler = AsyncIOScheduler()
templates = Jinja2Templates(directory="templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global config
    config = load_config(CONFIG_PATH)
    await init_db(DB_PATH)

    schedule_str = config["scan"].get("schedule", "02:00")
    hour, minute = map(int, schedule_str.split(":"))
    scheduler.add_job(_scheduled_scan, "cron", hour=hour, minute=minute)
    scheduler.start()

    yield

    scheduler.shutdown()


async def _scheduled_scan():
    global _scan_task
    if scan_state["running"]:
        return
    _scan_task = asyncio.create_task(run_scan(config, DB_PATH, scan_state))


app = FastAPI(lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/scan", response_class=HTMLResponse)
async def scan_page(request: Request):
    return templates.TemplateResponse("scan.html", {"request": request})


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/scan/start")
async def scan_start():
    global _scan_task
    if scan_state["running"]:
        return {"status": "already_running"}
    _scan_task = asyncio.create_task(run_scan(config, DB_PATH, scan_state))
    return {"status": "started"}


@app.post("/api/scan/pause")
async def scan_pause():
    if not scan_state["running"]:
        return {"status": "not_running"}
    scan_state["paused"] = True
    return {"status": "paused"}


@app.post("/api/scan/resume")
async def scan_resume():
    scan_state["paused"] = False
    return {"status": "resumed"}


@app.get("/api/scan/status")
async def scan_status(request: Request):
    async def event_generator():
        while True:
            if await request.is_disconnected():
                break
            yield {"data": json.dumps(scan_state)}
            await asyncio.sleep(2)

    return EventSourceResponse(event_generator())


@app.get("/api/scan/errors")
async def scan_errors():
    rows = await get_failed_files(50, DB_PATH)
    return [{"filepath": r[0], "error_msg": r[1]} for r in rows]


@app.get("/api/search")
async def search_api(
    q: str = Query(...),
    ext: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
):
    per_page = config.get("search", {}).get("results_per_page", 20)
    ext_filter = ext if ext and ext != "all" else None
    try:
        results, total = await run_search(q, ext_filter, page, per_page, DB_PATH)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

    pages = math.ceil(total / per_page) if per_page else 1

    def fmt_ts(ts):
        if ts is None:
            return None
        try:
            return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:
            return str(ts)

    for r in results:
        r["indexed_at"] = fmt_ts(r.get("indexed_at"))

    return {
        "query": q,
        "total": total,
        "page": page,
        "pages": pages,
        "results": results,
    }
