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

## 使用（默认简易模式，不用写代码）

双击 `frontend/index.html`（`file://` 打开）。服务没启动会弹窗给出两条启动命令和复制按钮，启动后点「重试」。也可以直接访问 `http://127.0.0.1:8820/`。

**三步就能用**：① 选抓取方式 → ② 粘贴一个或多个网址（每行一个）→ ③ 点「开始抓取」。不需要 CSS 选择器、container 或任何字段规则。

| 抓取方式 | `preset` | 输出字段 | 实现方式 |
|---|---|---|---|
| 智能提取新闻正文 | `article` | `url` / `title` / `content` | 启发式挑选语义容器（`article`、`main`、`[role=main]`、常见正文 class），按文本长度 / 段落数 / 链接密度打分，剔除 `nav` / `aside` / `footer` / `script` 等噪音，再用 `markdownify` 转成干净 Markdown |
| 提取页面所有文字 | `text` | `url` / `text` | `page.get_all_text()`，只忽略 `script` / `style` |
| 提取页面所有链接 | `links` | `page_url` / `link_text` / `link_url` | `page.css("a[href]")` + `page.urljoin()`，自动补全相对地址并去重 |
| 自定义规则（高级） | `custom` | 由 `fields` 决定 | 原来的 CSS / JSON 规则引擎，见下一节 |

正文与文字模式默认导出 **Markdown**，链接与自定义模式默认导出 **JSON**；下拉里还能选 JSON / JSONL / CSV / XML。表格里长正文只显示前 240 字（鼠标悬停看全文），**导出文件里是完整内容**。

前端 `Origin: null` 的 CORS 已在后端放行。若浏览器对 `file://` 请求本地端口有额外拦截，请改用 `http://127.0.0.1:8820/` 同一份页面。

界面行为：

- 原来那堆高级配置（链接范围、container、白黑名单、硬刹车、JSON 规则编辑器）都收进左栏底部的「高级 / 开发者模式」折叠面板，默认不展开；选「自定义规则」时会自动展开。
- 选「只抓这些网址」（单页模式）时，「跟随页面链接继续爬」会自动置灰禁用。
- 起始网址可以填多行，单页模式会按 1/2/3… 逐个抓取，某个网址失败不影响其余网址。
- 任务结束但提取到 0 条记录时，日志会用黄色提示；预设模式提示「请换一个抓取方式，或确认网址能正常打开」，自定义模式提示「请检查 CSS 选择器是否正确」。若有网址抓取失败，另用红色提示失败数量。
- 任务正常完成时日志输出「已完成，共 X 条」；取消或触发刹车时输出「任务结束（原因），共 X 条」。

## 高级：自定义 JSON 规则

只有选「自定义规则（高级）」时下面这套规则才生效。预设模式下 `container` / `fields` 会被忽略，`fields` 甚至可以为空。

```json
{
  "mode": "single",
  "preset": "custom",
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
  "follow": false,
  "domain_allowlist": [],
  "domain_blocklist": []
}
```

- `preset`：`article` / `text` / `links` 走零配置预设，不需要任何选择器；`custom` 或不填则走下面的自定义规则（此时 `fields` 必填）。
- `mode`：`single` **逐个抓取 `start_urls` 里的每一个网址**（某个网址失败只记警告并继续下一个）；`spider` 顺着链接持续爬。
- `start_urls`：每行/每项一个网址。单页模式支持多个，响应字节与请求数按域名累加统计。
- `scope`：`same_site` 只留在同站；`discover_domains` 允许发现新域名（受 `max_domains` 限制）。仅整站模式生效。
- `same_site_mode`：`host` 严格同 host；`registered_domain` 允许同一注册域下所有子域。仅整站模式生效。
- `container`：**后端确实使用**。填了就把每个页面按这个 CSS 选择器切成多块，每块产出一条记录，字段选择器写**相对路径**（默认示例 `.quote` + `.text::text`）；删掉或留空则按整页提取各字段并逐行 zip，此时选择器要写完整路径（如 `.quote .text::text`）。两种写法混用是 0 结果的最常见原因。
- `fields[*].type`：`string` / `number` / `array_string` / `array_number` / `html`。`array_*` 建议配合 `container` 使用（取一个容器块内的全部值）；无 container 时按整页逐行 zip，每个值单独成一条记录，array 字段会是单元素列表。
- `fields[*].postprocess_js`：JS 函数体，参数为 `value`，必须 `return`。在 Node `vm` 沙箱里执行，超时 100ms，出错回退原值。
- `follow`：仅整站模式生效。选「单页抓取」时前端会置灰禁用该勾选框，并强制提交 `false`。
- `limits`：三项硬刹车（页数 / 流量字节 / 域名数）任一触发即停止。单页模式同样受 `max_pages` / `max_bytes` 约束，但不检查域名数。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 前端启动检测 |
| GET | `/` | 返回同一份前端页面 |
| POST | `/api/tasks` | 提交规则，返回 `task_id` |
| GET | `/api/tasks/{id}/stream` | SSE：`connected` / `item` / `stats` / `log` / `done` / `task_error` |
| POST | `/api/tasks/{id}/cancel` | 协作式取消 |
| GET | `/api/tasks/{id}/status` | 轮询兜底 |
| GET | `/api/tasks/{id}/result?format=md\|json\|jsonl\|csv\|xml` | 导出，同时写入 `output/{id}/` |

自定义错误事件用 `task_error` 而不是 `error`，避免和 EventSource 原生断连事件冲突。

## 与第三方方案的差异（按仓库源码修正）

1. Scrapling 0.5.1 的 `name`、`start_urls`、`allowed_domains`、`concurrent_requests`、`download_delay`、`robots_txt_obey` 都是 **Spider 类属性**，构造参数只有 `crawldir` / `interval`。后端动态生成 `UISpider` 子类并赋类属性。
2. `ItemList.to_json()/to_csv()` 等**只写文件、返回 None**，所以导出用标准库自行序列化，不依赖 `ItemList`。
3. `Request` 的参数是 `meta`，不是 `metadata`。
4. `spider.stats` 只能在 `async for item in spider.stream():` 期间读取，结束后抛 `RuntimeError`；后端用 `to_dict()` 归一化并缓存快照。
5. `Fetcher.get()` 是同步阻塞方法，用 `asyncio.to_thread` 调用，避免卡住事件循环；`Spider.start()` 内部用 `anyio.run`，Web 服务里一律用 `stream()`。
6. `page.css(...).getall()` 返回 `TextHandlers`（str 子类），已统一转成 `str` 再序列化。
7. SSE 用 FastAPI 原生 `StreamingResponse`，不引入 `sse-starlette`；JS 沙箱用 Node `vm`，不引入 `py_mini_racer`。
8. `main_content_only` 不是正文抽取（只是 `<body>`），仓库内也没有 readability 类实现，所以简易模式的正文识别由 `backend/presets.py` 自己做；纯文本用 `get_all_text()`、链接用 `css("a[href]")` + `urljoin()`，都是 Scrapling 原生 API。
9. `Spider.stream()` 是 async generator：首次 `__anext__()` 里 `set_logger()` 返回 ContextVar Token，`finally` 里 `reset_logger(token)` **必须在创建它的同一个 Context 执行**；引擎的 `_stream()` 还把 anyio TaskGroup 跨 `yield` 持有，要求每次 `__anext__()` 都在**同一个 Task** 里跑。早期「每取一条数据就新建一个 Task 调 `__anext__()`」会在收尾时报 `<Token var=<ContextVar name='scrapling_logger'...> was created in a different Context`，把成功完成的爬取误判成 `task_error`。现在 `backend/worker.py` 用一个带 `contextvars.copy_context()` 的独立 Task 驱动整条流，再用 `asyncio.Queue` 把结果交给消费协程；单页 / 隐身浏览器抓取则在独立线程 + 独立 Context 中执行。收尾只由那个独立 Task 完成，绝不从别的 Task 调 `generator.aclose()`。

## 已知限制

- 「智能正文」是**启发式打分**，不是 readability / trafilatura：对绝大多数新闻站有效，但结构特别奇怪的页面可能仍混入导航或漏掉正文，这时换「提取页面所有文字」或「自定义规则」。
- 没有引入 `readability-lxml`：它强依赖 `cssselect<1.4`，与 Scrapling 要求的 `cssselect>=1.5` 冲突，会破坏 CSS 选择器。正文识别在 `backend/presets.py` 自行实现，Markdown 转换用 `markdownify`（已写入 `requirements.txt`）。
- Scrapling 自带的 `page.markdown(main_content_only=True)` 只是取 `<body>`，**不会去广告**，所以预设模式没有直接依赖它。
- 只绑定 `127.0.0.1`，不要改成 `0.0.0.0`。放行 `Origin: null` 意味着任何本地 HTML 文件都能调用本 API，请勿在本机打开来路不明的 HTML。
- 私有 IP / `localhost` / `.local` 等字面地址会被拒绝；未做域名 DNS 解析后的 IP 检查，跨域发现模式请自行配好白名单。
- 「发现更多域名」默认允许跟随外链，请遵守目标站 ToS 与 robots.txt，不要高频压站。
- 取消是协作式：取消事件与「队列里的下一条数据」竞争等待，因此即使引擎还没产出任何结果也能立即停止；同时调用引擎的 `pause()` 两次触发强制停止，`_stop_flag` 让 `parse()` 不再跟随新链接。整站流的 async generator 只在那个独立 Task 内收尾（最多等 10 秒，超时才取消 driver），不会从别的 Task 关闭它。已在队列中的少量请求可能仍会完成。
- 单页 HTTP 请求超时 15 秒（浏览器模式沿用 Scrapling 默认 30 秒）；整站模式沿用引擎默认 30 秒，另有页数/流量/域名三项刹车。
- 不要在短时间内对同一站点反复跑测试，容易被限流导致请求超时。
- 浏览器模式每次抓取都要启动 Chromium，慢且占资源；默认关闭。若日志出现 `Executable doesn't exist at .../chromium-XXXX/`，说明 Playwright/Patchright 升级后缺少对应版本的浏览器，执行 `./.venv/bin/patchright install chromium`（或 `./.venv/bin/scrapling install`）补齐；单页模式会把该错误记为跳过并继续其余网址。
- `output/`、`.venv/` 已被 `webui/.gitignore` 忽略，不要提交抓取结果或登录态。
