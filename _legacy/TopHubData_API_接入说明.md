# TopHub Data（今日热榜/榜眼数据）API 接入说明（给团队维护）

> 用途：给 Hotspot-Spark 提供“抖音热榜清单”（标题 + 可打开链接等），再由本机脚本/后端去做后续增强（解析 MP4、Gemini 提取）。
>
> 官方文档：`https://www.tophubdata.com/documentation`

---

## 1. 核心事实（避免误解）

- TopHub Data **只抓榜单页面**：通常只有 `title` 和 `url`（可能还有 `description/thumbnail/extra/time`），**不会自动抓正文/视频内容**。
- 所以 TopHub Data 在我们的链路里只负责“**拿清单**”，正文/画面/文案/语音转写都在后续环节完成。

---

## 2. 鉴权方式（必须）

- 请求域名：`https://api.tophubdata.com/`
- 认证方式：HTTP Header
  - `Authorization: YOUR_ACCESS_KEY`
- 重要建议：不要把 Access Key 明文写进仓库或文档；统一放在：
  - Cloudflare Pages/Workers 的 Secret（线上）
  - 本机 `.dev.vars` / 环境变量（本地）

> 你的 Access Key 我已知，但不写入此文档，避免泄露与误提交。

---

## 3. 我们实际用到的接口（够维护用）

### 3.1 获取全部榜单列表（找 hashid）

- `GET https://api.tophubdata.com/nodes`
- 参数（可选）：
  - `p`：页码（每页 100 条）
- 用法：用 `domain/name/display` 找到目标榜单的 `hashid`（注意区分大小写）。

示例（cURL）：
```bash
curl --location "https://api.tophubdata.com/nodes?p=1" --header "Authorization: $TOPHUB_API_KEY"
```

### 3.2 获取单个榜单历史数据（我们目前在用）

- `GET https://api.tophubdata.com/nodes/{hashid}/historys?date=YYYY-MM-DD`
- 返回：`data` 数组（每条含 `title/url/extra/time/thumbnail/...`，不同榜单字段可能略有差异）

示例（cURL）：
```bash
curl --location "https://api.tophubdata.com/nodes/DpQvNABoNE/historys?date=2026-01-01" --header "Authorization: $TOPHUB_API_KEY"
```

> 说明：`DpQvNABoNE` 是当前 Hotspot-Spark 对“抖音”使用的默认 hashid（可在配置里改）。

---

## 4. 错误码（排障用）

常见错误码（摘自官方文档）：
- `100101`：缺少参数或参数无效
- `100201`：未授权（Authorization 错误）
- `100202`：请求 IP 受限（开启了白名单）
- `100300`：余额不足
- `100500`：内部错误

---

## 5. 在我们项目里的落地点（谁在用、怎么改）

### 5.1 Hotspot-Spark：负责“向 TopHub 拉清单并缓存”

- 代码入口：`E:\cursor\Hotspot-Spark\functions\api\hotlist\douyin.ts`
- 对外接口：`GET https://hot-sparks.jacky.onl/api/hotlist/douyin?date=YYYY-MM-DD&refresh=1`
- 行为：
  - `refresh=1` 或缓存太少时 → 触发从 TopHub 现拉
  - 拉到的数据会缓存进 R2（避免反复扣费/不稳定）

需要配置的变量/密钥（Hotspot-Spark）：
- `TOPHUB_DOUYIN_HASHID`（变量，默认 `DpQvNABoNE`）
- `TOPHUB_API_KEY`（密钥/Secret，不要提交到 git）

`wrangler.toml.example` 在这里：`E:\cursor\Hotspot-Spark\wrangler.toml.example`

### 5.2 TrendRadar（本机）：负责“每天跑增强任务”

- 入口脚本：`E:\cursor\TrendRadar\run-daily-newsource-r2.ps1`
- 说明：`E:\cursor\TrendRadar\NEW_SOURCE_TASK.md`
- 逻辑：
  1) 调 Hotspot-Spark 的 `/api/hotlist/douyin` 拉当天抖音清单
  2) 调本机 `video_spider` 解析出可下载 `mp4_url`
  3) 批量 `POST /api/douyin/ingest` 触发下载+Gemini 提取，结果写入 R2

结论：
- **团队日常维护通常不需要直接调 TopHub**，只需要保证 Hotspot-Spark 的 `TOPHUB_API_KEY` 有效即可。

