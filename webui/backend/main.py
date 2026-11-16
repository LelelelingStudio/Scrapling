"""FastAPI entry point for the Scrapling local web UI (127.0.0.1:8820)."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse

from backend.exports import FORMATS, render, save
from backend.rules import Spec
from backend.worker import manager

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "frontend" / "index.html"

app = FastAPI(title="Scrapling Local UI", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "null",  # file:// 双击打开的页面
        "http://127.0.0.1:8820",
        "http://localhost:8820",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "port": 8820}


@app.get("/")
async def index() -> Response:
    if not INDEX.exists():
        raise HTTPException(500, f"找不到前端文件: {INDEX}")
    return FileResponse(INDEX)


@app.post("/api/tasks")
async def create_task(spec: Spec) -> dict:
    try:
        task = manager.create(spec)
    except RuntimeError as exc:
        raise HTTPException(429, str(exc)) from exc
    return {"task_id": task.id}


@app.get("/api/tasks/{task_id}/stream")
async def stream(task_id: str) -> StreamingResponse:
    task = manager.get(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return StreamingResponse(
        manager.event_stream(task),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str) -> dict:
    if manager.get(task_id) is None:
        raise HTTPException(404, "任务不存在")
    return {"cancelled": manager.cancel(task_id)}


@app.get("/api/tasks/{task_id}/status")
async def task_status(task_id: str) -> dict:
    task = manager.get(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return {
        "task_id": task.id,
        "status": task.status,
        "reason": task.reason,
        "item_count": len(task.items),
        "stats": task.stats,
    }


@app.get("/api/tasks/{task_id}/result")
async def task_result(task_id: str, format: str = Query("json")) -> Response:
    task = manager.get(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    fmt = format.lower()
    if fmt not in FORMATS:
        raise HTTPException(422, f"仅支持 {', '.join(FORMATS)}")

    content, media_type = render(task.items, fmt)
    try:
        save(task_id, task.items, fmt)
    except OSError:
        pass
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="scrape-{task_id}.{fmt}"'},
    )
