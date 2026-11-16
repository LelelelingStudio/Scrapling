"""Task lifecycle: single-page fetch, spider streaming, SSE events and brakes."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Callable

from . import stats as statsmod
from .presets import extract_preset
from .rules import (
    Spec,
    allowed_domains_for,
    apply_postprocess,
    extract_items,
    host_of,
    url_allowed,
)

TERMINAL = ("done", "task_error")


def sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@dataclass
class Task:
    id: str
    spec: Spec
    status: str = "running"
    reason: str | None = None
    items: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=statsmod.empty)
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    wake: asyncio.Queue = field(default_factory=asyncio.Queue)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    job: asyncio.Task | None = None
    request_stop: Callable[[bool], None] | None = None

    def emit(self, event: str, data: dict[str, Any] | None = None) -> None:
        self.events.append((event, data or {}))
        self.wake.put_nowait(len(self.events))


class TaskManager:
    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}

    def get(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)

    def create(self, spec: Spec) -> Task:
        running = sum(1 for t in self.tasks.values() if t.status == "running")
        if running >= 3:
            raise RuntimeError("同时最多运行 3 个任务")
        task = Task(id=uuid.uuid4().hex[:12], spec=spec)
        self.tasks[task.id] = task
        task.job = asyncio.create_task(run(task))
        return task

    def cancel(self, task_id: str) -> bool:
        task = self.get(task_id)
        if task is None or task.status != "running":
            return False
        task.cancel.set()
        if task.request_stop:
            try:
                task.request_stop(True)  # force: engine-level immediate stop
            except Exception:
                pass
        return True

    async def event_stream(self, task: Task) -> AsyncGenerator[str, None]:
        """Replay existing events first, then stream new ones (survives reconnect)."""
        index = 0
        yield sse("connected", {"task_id": task.id})
        while True:
            while index < len(task.events):
                event, data = task.events[index]
                index += 1
                yield sse(event, data)
                if event in TERMINAL:
                    return
            try:
                await asyncio.wait_for(task.wake.get(), timeout=15)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"


manager = TaskManager()


def _finish(task: Task, reason: str) -> None:
    task.status = "cancelled" if reason == "cancelled" else "done"
    task.reason = reason
    task.request_stop = None
    task.emit("stats", task.stats)
    task.emit(
        "done",
        {
            "reason": reason,
            "total_items": len(task.items),
            "total_bytes": task.stats.get("response_bytes", 0),
            "failed": task.stats.get("failed", 0),
        },
    )


async def _cancel_watch(task: Task) -> None:
    """Force-stop the engine as soon as it exists, even before any item arrives."""
    await task.cancel.wait()
    for _ in range(200):
        callback = task.request_stop
        if callback is not None:
            try:
                callback(True)
            except Exception:
                pass
            return
        if task.status != "running":
            return
        await asyncio.sleep(0.05)


async def run(task: Task) -> None:
    watcher = asyncio.create_task(_cancel_watch(task))
    try:
        if task.spec.mode == "single":
            await _run_single(task)
        else:
            await _run_spider(task)
    except Exception as exc:
        task.emit("log", {"level": "error", "message": f"任务异常: {exc}"})
        task.emit("task_error", {"message": str(exc)})
        task.status = "error"
        task.reason = str(exc)
    finally:
        watcher.cancel()


async def _run_single(task: Task) -> None:
    """Fetch every start_url in order; one failing URL must not abort the rest."""
    spec = task.spec
    pages: list[tuple[str, int]] = []
    failed = 0

    def _fetch(url: str) -> Any:
        from scrapling.fetchers import Fetcher, StealthyFetcher

        if spec.browser:
            return StealthyFetcher.fetch(url)
        # Scrapling defaults to 30s; keep single-page tasks from hanging too long.
        return Fetcher.get(url, timeout=15)

    total_urls = len(spec.start_urls)
    for index, url in enumerate(spec.start_urls, 1):
        if task.cancel.is_set():
            break
        task.emit("log", {"level": "info", "message": f"单页抓取 {index}/{total_urls}：{url}"})
        try:
            page = await asyncio.to_thread(_fetch, url)
        except Exception as exc:
            failed += 1
            task.emit("log", {"level": "warn", "message": f"跳过 {url}：{exc}"})
            continue

        try:
            if spec.preset and spec.preset != "custom":
                extracted = await asyncio.to_thread(extract_preset, page, url, spec.preset)
            else:
                extracted = extract_items(page, spec)
        except Exception as exc:
            task.emit("log", {"level": "error", "message": f"{url} 提取失败: {exc}"})
            extracted = []

        for item in extracted:
            item, warning = await apply_postprocess(item, spec)
            if warning:
                task.emit("log", {"level": "warn", "message": warning})
            task.items.append(item)
            task.emit("item", item)

        try:
            byte_count = len(page.body)
        except Exception:
            byte_count = 0
        pages.append((url, byte_count))

        hit = statsmod.limits_reached(
            statsmod.single(pages, len(task.items), failed),
            spec.limits,
            check_domains=False,
        )
        if hit:
            task.emit("log", {"level": "warn", "message": f"触发刹车：{hit}，停止后续网址"})
            break

    task.stats = statsmod.single(pages, len(task.items), failed)
    if failed and not pages:
        task.emit(
            "log",
            {
                "level": "error",
                "message": (
                    f"全部 {failed} 个网址抓取失败。请检查网络或 URL；"
                    "若提示 ModuleNotFoundError，请在 webui/.venv 中执行 pip install -e \"..[fetchers]\"；"
                    "浏览器模式还需 ./.venv/bin/scrapling install"
                ),
            },
        )
    _finish(task, "cancelled" if task.cancel.is_set() else "completed")


async def _run_spider(task: Task) -> None:
    if task.cancel.is_set():
        _finish(task, "cancelled")
        return
    spec = task.spec
    start_host = host_of(spec.start_urls[0])

    try:
        from scrapling.spiders import LinkExtractor, Spider
    except Exception as exc:
        message = f"无法导入 Scrapling spiders: {exc}。请先执行 pip install -e \"..[fetchers]\""
        task.emit("log", {"level": "error", "message": message})
        task.emit("task_error", {"message": message})
        task.status = "error"
        task.reason = "import_failed"
        return

    link_extractor = LinkExtractor()

    # Scrapling v0.5.1: these settings are CLASS attributes, not __init__ kwargs.
    class UISpider(Spider):
        name = "ui_" + task.id
        start_urls = spec.start_urls
        allowed_domains = allowed_domains_for(spec, start_host)
        concurrent_requests = spec.limits.concurrent_requests
        download_delay = spec.limits.download_delay
        robots_txt_obey = spec.limits.robots_txt_obey

        _spec = spec
        _links = link_extractor
        _start_host = start_host
        _task = task
        _stop_flag = False

        async def parse(self, response):
            if self._stop_flag:
                return
            try:
                if self._spec.preset and self._spec.preset != "custom":
                    extracted = await asyncio.to_thread(
                        extract_preset, response, str(response.url), self._spec.preset
                    )
                else:
                    extracted = extract_items(response, self._spec)
            except Exception as exc:
                self._task.emit("log", {"level": "warn", "message": f"提取失败: {exc}"})
                extracted = []
            for item in extracted:
                yield item

            if self._stop_flag or not self._spec.follow:
                return
            try:
                links = self._links.extract(response)
            except Exception:
                return
            for url in links:
                if url_allowed(url, self._spec, self._start_host):
                    yield response.follow(url)

    spider = UISpider()
    pause_calls = 0

    def stop_spider(force: bool = False) -> None:
        nonlocal pause_calls
        spider._stop_flag = True
        target = 2 if force else 1  # 1 = graceful, 2nd call = force stop in the engine
        try:
            while pause_calls < target:
                spider.pause()
                pause_calls += 1
        except Exception:
            pass

    task.request_stop = stop_spider
    task.emit(
        "log",
        {
            "level": "info",
            "message": f"整站模式启动：scope={spec.scope}, 同站判定={spec.same_site_mode}, 起始域名={start_host}",
        },
    )

    generator = spider.stream()
    last_push = 0.0
    reason = "completed"
    stopped = False
    try:
        while True:
            if task.cancel.is_set():
                reason = "cancelled"
                stop_spider(True)
                stopped = True
                break

            # Race the next item against the cancel event: cancellation must work
            # even when the engine has not produced a single item yet.
            next_item = asyncio.ensure_future(generator.__anext__())
            cancelled = asyncio.ensure_future(task.cancel.wait())
            done, pending = await asyncio.wait(
                {next_item, cancelled}, return_when=asyncio.FIRST_COMPLETED
            )
            for pending_task in pending:
                pending_task.cancel()
            if cancelled in done:
                reason = "cancelled"
                stop_spider(True)
                stopped = True
                break
            try:
                item = next_item.result()
            except StopAsyncIteration:
                break

            item, warning = await apply_postprocess(item, spec)
            if warning:
                task.emit("log", {"level": "warn", "message": warning})
            task.items.append(item)
            task.emit("item", item)

            try:
                task.stats = statsmod.normalize(spider.stats.to_dict())
            except RuntimeError:
                pass

            now = time.monotonic()
            if now - last_push >= 0.5:
                task.emit("stats", task.stats)
                last_push = now

            hit = statsmod.limits_reached(
                task.stats, spec.limits, check_domains=spec.scope == "discover_domains"
            )
            if hit:
                reason = hit
                task.emit("log", {"level": "warn", "message": f"触发刹车：{hit}"})
                stop_spider(False)
                stopped = True
                break
    except BaseException as exc:
        task.emit("log", {"level": "error", "message": f"爬取中断: {exc}"})
        reason = f"error: {exc}"
        stopped = True

    if stopped:
        # Never call generator.aclose() here: Scrapling's engine wraps the stream in
        # an anyio TaskGroup, and closing it from another task raises
        # "Attempted to exit cancel scope in a different task than it was entered in".
        # Keep a reference so the generator is not garbage-collected mid-crawl.
        task._generator = generator  # type: ignore[attr-defined]

    if task.cancel.is_set() and reason == "completed":
        reason = "cancelled"
    _finish(task, reason)
