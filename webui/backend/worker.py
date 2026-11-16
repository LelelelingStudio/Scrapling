"""Task lifecycle: single-page fetch, spider streaming, SSE events and brakes."""
from __future__ import annotations

import asyncio
import contextvars
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
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
# Scrapling resolves a relative crawldir against the process CWD, which is not
# guaranteed when uvicorn is started from a .command file. Use an absolute path.
WEBUI_ROOT = Path(__file__).resolve().parent.parent
CRAWL_DATA_DIR = WEBUI_ROOT / "crawl_data"
CHECKPOINT_FILE = "checkpoint.pkl"
# Scrapling's default periodic snapshot is 300s; short UI crawls would never
# write one. 15s makes brakes/cancellations durable without much I/O.
CHECKPOINT_INTERVAL = 15.0


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
    crawl_dir: Path | None = None

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
    if reason == "completed":
        task.emit("log", {"level": "info", "message": f"已完成，共 {len(task.items)} 条"})
    else:
        task.emit("log", {"level": "info", "message": f"任务结束（{reason}），共 {len(task.items)} 条"})
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
    if spec.crawl_resume:
        task.emit(
            "log",
            {"level": "warn", "message": "断点续爬只对整站模式生效，单页模式已忽略该选项"},
        )

    def _fetch(url: str) -> Any:
        from scrapling.fetchers import Fetcher, StealthyFetcher

        def _call() -> Any:
            if spec.browser:
                return StealthyFetcher.fetch(url)
            # Scrapling defaults to 30s; keep single-page tasks from hanging too long.
            return Fetcher.get(url, timeout=15)

        # StealthyFetcher drives Playwright in this worker thread. Run it in an
        # isolated contextvars.Context so any Scrapling logger override stays
        # here instead of leaking back into the FastAPI request context.
        return contextvars.copy_context().run(_call)

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

    crawl_dir: Path | None = None
    checkpoint: Path | None = None
    had_checkpoint = False
    label = ""
    if spec.crawl_resume:
        # The host is already validated as a public http(s) host; keep the
        # directory name filesystem-safe and free of path traversal anyway.
        safe_host = re.sub(r"[^A-Za-z0-9._-]", "_", start_host).strip("._") or "site"
        crawl_dir = CRAWL_DATA_DIR / safe_host
        checkpoint = crawl_dir / CHECKPOINT_FILE
        had_checkpoint = checkpoint.exists()
        label = f"crawl_data/{safe_host}"
        try:
            crawl_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            message = f"无法创建断点目录 {crawl_dir}：{exc}"
            task.emit("log", {"level": "error", "message": message})
            task.emit("task_error", {"message": message})
            task.status = "error"
            task.reason = "crawldir_failed"
            return
        # checkpoint.pkl has a fixed name, so two concurrent crawls on the same
        # host would silently overwrite each other's progress.
        for other in manager.tasks.values():
            if other is not task and other.status == "running" and (
                other.crawl_dir == crawl_dir
                # The other run() may not have been scheduled yet, so its
                # crawl_dir is still None; match on the spec as a fallback.
                or (
                    other.spec.crawl_resume
                    and other.spec.start_urls
                    and host_of(other.spec.start_urls[0]) == start_host
                )
            ):
                message = "同一域名的断点续爬任务已在运行，请等它结束再启动，避免进度互相覆盖"
                task.emit("log", {"level": "error", "message": message})
                task.emit("task_error", {"message": message})
                task.status = "error"
                task.reason = "crawldir_busy"
                return
        task.crawl_dir = crawl_dir
        if had_checkpoint:
            task.emit(
                "log",
                {"level": "info", "message": f"正在从本地恢复上次的进度...（{label}）"},
            )
        else:
            task.emit(
                "log",
                {"level": "info", "message": f"未发现上次的进度，本次从头开始；进度会保存到 {label}"},
            )

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
    # It has no engine-level max_pages/max_bytes attributes; those brakes are
    # enforced below against the live spider.stats after every yielded item.
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

        async def on_start(self, resuming: bool = False) -> None:
            # CrawlerEngine.crawl() calls this AFTER trying to load the
            # checkpoint, so `resuming` reflects the real engine state.
            if resuming:
                self._task.emit(
                    "log",
                    {"level": "info", "message": "引擎已载入上次队列：跳过起始网址，继续抓取剩余链接"},
                )
            elif had_checkpoint:
                self._task.emit(
                    "log",
                    {"level": "warn", "message": "本地进度文件无法载入，本次将从头开始"},
                )

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

    # crawldir=None disables checkpoints entirely; interval only matters when
    # resume is enabled, where 15s snapshots survive short UI crawls.
    spider = UISpider(crawldir=crawl_dir, interval=CHECKPOINT_INTERVAL)
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
            "message": (
                f"整站模式启动：scope={spec.scope}, 同站判定={spec.same_site_mode}, 起始域名={start_host}；"
                f"本次硬刹车 {spec.limits.max_pages} 页 / {spec.limits.max_bytes} 字节 / "
                f"{spec.limits.max_domains} 个域名"
            ),
        },
    )

    # Scrapling's Spider.stream() is an async generator: its first __anext__()
    # calls set_logger() and its finally block calls reset_logger(token). A
    # ContextVar Token may ONLY be reset in the exact Context that created it.
    # Driving __anext__() from a fresh Task for every item (the old code) made
    # the Token belong to one throwaway Task while the reset happened in the
    # next one, raising
    # "<Token var=<ContextVar name='scrapling_logger'...> was created in a
    # different Context" and turning a finished crawl into a task_error.
    # The engine's _stream() also holds an anyio TaskGroup across yields, which
    # requires every __anext__() to run in the SAME task.
    # Fix: drive the entire stream in ONE dedicated Task with its own isolated
    # contextvars.Context, and hand items to this consumer through a Queue.
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    async def drive_stream() -> None:
        generator = spider.stream()
        # Keep a reference even after cancellation: GC'ing a half-consumed async
        # generator would run its finally block in yet another Context.
        task._stream_generator = generator  # type: ignore[attr-defined]
        try:
            while True:
                try:
                    item = await generator.__anext__()
                except StopAsyncIteration:
                    break
                queue.put_nowait(("item", item))
        except BaseException as exc:  # noqa: BLE001 - forward to the consumer
            queue.put_nowait(("error", exc))
        finally:
            queue.put_nowait(("done", None))

    spider_context = contextvars.copy_context()
    stream_coro = drive_stream()
    try:
        driver = asyncio.Task(stream_coro, context=spider_context)  # Python 3.11+
    except TypeError:  # Python 3.10: create_task() already copies the context
        driver = asyncio.create_task(stream_coro)

    last_push = 0.0
    reason = "completed"
    try:
        while True:
            if task.cancel.is_set():
                reason = "cancelled"
                stop_spider(True)
                break

            # Race the next queued item against the cancel event: cancellation
            # must work even before the engine has produced a single item.
            getter = asyncio.ensure_future(queue.get())
            cancelled = asyncio.ensure_future(task.cancel.wait())
            done, pending = await asyncio.wait(
                {getter, cancelled}, return_when=asyncio.FIRST_COMPLETED
            )
            for pending_task in pending:
                pending_task.cancel()
            if cancelled in done:
                reason = "cancelled"
                stop_spider(True)
                break

            kind, payload = getter.result()
            if kind == "error":
                raise payload if isinstance(payload, BaseException) else RuntimeError(str(payload))
            if kind == "done":
                break

            item, warning = await apply_postprocess(payload, spec)
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
                break
    except BaseException as exc:
        task.emit("log", {"level": "error", "message": f"爬取中断: {exc}"})
        reason = f"error: {exc}"
    finally:
        # Let the driver tear down inside its OWN context. Never call
        # generator.aclose() from here (a different Task): Scrapling's engine
        # wraps the stream in an anyio TaskGroup whose cancel scope would then
        # raise "Attempted to exit cancel scope in a different task...".
        try:
            # Graceful pause waits for in-flight requests before writing the
            # checkpoint; give resumable crawls extra time instead of cancelling
            # the driver (a cancelled driver never saves a snapshot).
            await asyncio.wait_for(asyncio.shield(driver), timeout=30 if crawl_dir else 10)
        except asyncio.CancelledError:
            driver.cancel()
            raise
        except asyncio.TimeoutError:
            driver.cancel()
            try:
                await driver
            except BaseException as exc:
                task.emit("log", {"level": "warn", "message": f"引擎收尾超时：{exc}"})
        except BaseException as exc:
            task.emit("log", {"level": "error", "message": f"引擎收尾异常: {exc}"})

    if checkpoint is not None:
        if checkpoint.exists():
            task.emit(
                "log",
                {"level": "info", "message": f"进度已保存到 {label}，下次勾选「断点续爬」可接着跑"},
            )
        elif had_checkpoint:
            task.emit(
                "log",
                {"level": "info", "message": "本次队列已跑完，引擎清除了本地进度；下次会从头开始"},
            )
        else:
            task.emit(
                "log",
                {"level": "warn", "message": "本次未生成进度快照（任务太短或异常中断），下次会从头开始"},
            )

    if task.cancel.is_set() and reason == "completed":
        reason = "cancelled"
    _finish(task, reason)
