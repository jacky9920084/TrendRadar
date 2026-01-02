# 进度状态（Hotspot-Spark × TrendRadar，新源增强）

更新时间：2026-01-01

## 目标架构（已定）

- **TrendRadar（本机）负责重活**：拉热点 →（抖音）解析链接/下载 MP4（仅临时文件）→ 调 Gemini Flash 提取 → 写入 R2（仅 JSON/文本）。
- **Hotspot-Spark（Cloudflare）做轻后端**：提供热点列表接口、Step3/Step4 选题分析、从 R2 读取材料并拼接到 Step3/Step4 输入；不再下载 MP4、不再在 CF 里跑 Gemini 提取。

## 关键材料（R2 Key 约定）

- 抖音（日材料）：`ai-materials/YYYY/MM/DD/douyin/{videoId}.json`
- 抖音（全局去重缓存）：`ai-materials/douyin-by-id/{videoId}.json`
- 网页（日材料）：`ai-materials/YYYY/MM/DD/web/{sha1}.json`

抖音材料 JSON 里至少应包含：

- `text`：文案/转写/可读文本
- `visual`：画面要点（给 Step3/Step4 用）
- `why_hot`：一句话“为什么会火”（给 Step3/Step4 用）
- `meta`：时间、来源、失败原因等（失败也要写占位，避免重复消耗）

## 当前线上确认（抖音热点数量）

已验证接口可用：

- `GET https://hot-sparks.jacky.onl/api/hotlist/douyin?date=2026-01-02&refresh=1`
  - 返回条数：**20**
  - `source=tophub_latest`（说明：当天 `historys` 为空时已自动兜底到“实时榜单”）

## 已修补点（你点名的 2 个）

1) `Hotspot-Spark /api/hotlist/douyin`

- 逻辑：先请求 TopHub `historys?date=YYYY-MM-DD`；如果为空 → 自动改用 `nodes/{hashid}`（实时）。
- 响应字段 `source` 会标明 `tophub_latest`，方便排查“为什么某天返回的是实时榜单”。

2) `TrendRadar run-daily-newsource-douyin-local.ps1`

- 逻辑：如果当前进程 `$env:GEMINI_API_KEY` 为空，会自动从 **用户级/机器级环境变量**兜底读取（也支持 `GOOGLE_API_KEY`）。

## 抖音增强脚本（本机）并发策略

- 默认并发：2
- 强制范围：1～3（超过会被压回 3，避免把本机/网络打穿）

建议首次试水：先用并发 1 跑通链路，再放大到 2～3。

## 抖音主源已切换（关键）

- 旧源 `hotspots.txt` 里的“抖音短链条目”已视为无效来源（不再进入主清单）。
- 每天生成完 `ai-hotspots/YYYY/MM/DD/hotspots.txt` 后，会用 TopHub 抖音榜单替换掉旧抖音条目：
  - 脚本：`E:\cursor\TrendRadar\run-daily-23-r2.ps1`（末尾会调用 `E:\cursor\TrendRadar\merge_douyin_into_hotspots.py`）
  - 结果：主清单里的抖音 URL 变为 `https://www.douyin.com/video/<id>`，从而能命中 `ai-materials/.../douyin/<id>.json` 的增强材料
