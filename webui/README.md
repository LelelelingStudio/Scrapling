# Scrapling 本地爬虫面板

零框架前端 + FastAPI 后端，只监听 `127.0.0.1:8820`，数据全部留在本机。

## 安装（只需一次）

```bash
cd /Users/david/Documents/GitHub/Scrapling/webui
/opt/homebrew/bin/python3.13 -m venv .venv
./.venv/bin/python -m pip install -U pip
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python -m pip install -e "..[fetchers]"
```

使用 Python 3.13 而不是系统默认的 3.15：Scrapling 0.5.1 声明支持 3.10~3.13。
`scrapling install` 只在使用「隐身浏览器渲染」时才需要（会下载 Chromium）：

```bash
./.venv/bin/scrapling install
```

`postprocess_js` 依赖本机已安装的 Node（通过 `node` 命令调用 `backend/js_sandbox.js`）。

## 启动

方式 A（推荐）：终端执行这两条命令

```bash
cd /Users/david/Documents/GitHub/Scrapling/webui
./.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8820
```

方式 B：双击 `start.command`。

## 使用

双击 `frontend/index.html`（`file://` 打开）。如果服务没启动，页面会弹窗显示上面两条命令和复制按钮；启动后点「重试」即可。服务启动后也可以直接访问 `http://127.0.0.1:8820/`。

前端 `Origin: null` 的 CORS 已在后端放行。若浏览器对 `file://` 请求本地端口有额外拦截，请改用 `http://127.0.0.1:8820/` 同一份页面。

## 规则 JSON

```json
{
  "mode": "single",
  "scope": "same_site",
  "same_site_mode": "host",
  "start_urls": ["https://quotes.toscrape.com/"],
  "container": ".quote",
  "fields": {
    "text":   { "selector": ".text::text", "type": "string" },
    "author": { "selector": ".author::text", "type": "string", "postprocess_js": "return String(value || '').toUpperCase();" },
    "tags":   { "selector": ".tag::text", "type": "array_string" }
  },
  "limits": {
    "max_pages": 50, "max_bytes": 8000000, "max_domains": 10,
    "concurrent_requests": 5, "download_delay": 0.5, "robots_txt_obey": true
  },
  "browser": false,
  "follow": true,
  "domain_allowlist": [],
  "domain_blocklist": []
}
```

- `mode`：`single` 抓一个页面；`spider` 顺着链接持续爬。
- `scope`：`same_site` 只留在同站；`discover_domains` 允许发现新域名（受 `max_domains` 限制）。
- `same_site_mode`：`host` 严格同 host；`registered_domain` 允许同一注册域下所有子域。
- `container`：可选。填了就把页面按这个选择器切成多块，每块产出一条记录，字段选择器写相对路径；不填则按各字段提取结果逐行 zip。
- `fields[*].type`：`string` / `number` / `array_string` / `array_number` / `html`。
- `fields[*].postprocess_js`：JS 函数体，参数为 `value`，必须 `return`。在 Node `vm` 沙箱里执行，超时 100ms，出错回退原值。
- `limits`：三项硬刹车（页数 / 流量字节 / 域名数）任一触发即停止。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 前端启动检测 |
| GET | `/` | 返回同一份前端页面 |
| POST | `/api/tasks` | 提交规则，返回 `task_id` |
| GET | `/api/tasks/{id}/stream` | SSE：`connected` / `item` / `stats` / `log` / `done` / `task_error` |
| POST | `/api/tasks/{id}/cancel` | 协作式取消 |
| GET | `/api/tasks/{id}/status` | 轮询兜底 |
| GET | `/api/tasks/{id}/result?format=json\|jsonl\|csv\|xml` | 导出，同时写入 `output/{id}/` |

自定义错误事件用 `task_error` 而不是 `error`，避免和 EventSource 原生断连事件冲突。

## 与第三方方案的差异（按仓库源码修正）

1. Scrapling 0.5.1 的 `name`、`start_urls`、`allowed_domains`、`concurrent_requests`、`download_delay`、`robots_txt_obey` 都是 **Spider 类属性**，构造参数只有 `crawldir` / `interval`。后端动态生成 `UISpider` 子类并赋类属性。
2. `ItemList.to_json()/to_csv()` 等**只写文件、返回 None**，所以导出用标准库自行序列化，不依赖 `ItemList`。
3. `Request` 的参数是 `meta`，不是 `metadata`。
4. `spider.stats` 只能在 `async for item in spider.stream():` 期间读取，结束后抛 `RuntimeError`；后端用 `to_dict()` 归一化并缓存快照。
5. `Fetcher.get()` 是同步阻塞方法，用 `asyncio.to_thread` 调用，避免卡住事件循环；`Spider.start()` 内部用 `anyio.run`，Web 服务里一律用 `stream()`。
6. `page.css(...).getall()` 返回 `TextHandlers`（str 子类），已统一转成 `str` 再序列化。
7. SSE 用 FastAPI 原生 `StreamingResponse`，不引入 `sse-starlette`；JS 沙箱用 Node `vm`，不引入 `py_mini_racer`。

## 已知限制

- 只绑定 `127.0.0.1`，不要改成 `0.0.0.0`。放行 `Origin: null` 意味着任何本地 HTML 文件都能调用本 API，请勿在本机打开来路不明的 HTML。
- 私有 IP / `localhost` / `.local` 等字面地址会被拒绝；未做域名 DNS 解析后的 IP 检查，跨域发现模式请自行配好白名单。
- 「发现更多域名」默认允许跟随外链，请遵守目标站 ToS 与 robots.txt，不要高频压站。
- 取消是协作式：取消事件与「下一条数据」竞争等待，因此即使引擎还没产出任何结果也能立即停止；同时调用引擎的 `pause()` 两次触发强制停止，`_stop_flag` 让 `parse()` 不再跟随新链接。已在队列中的少量请求可能仍会完成。
- 单页 HTTP 请求超时 15 秒（浏览器模式沿用 Scrapling 默认 30 秒）；整站模式沿用引擎默认 30 秒，另有页数/流量/域名三项刹车。
- 不要在短时间内对同一站点反复跑测试，容易被限流导致请求超时。
- 浏览器模式每次抓取都要启动 Chromium，慢且占资源；默认关闭。
- `output/`、`.venv/` 已被 `webui/.gitignore` 忽略，不要提交抓取结果或登录态。
