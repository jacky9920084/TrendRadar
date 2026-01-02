# TrendRadar 新源定时任务（抖音全本机提取 → 写 R2 材料）

目标：把“解析/下载视频/Gemini 提取/重试/日志”全部放在本机（TrendRadar）做，最终在 R2 写入：

- `ai-materials/YYYY/MM/DD/douyin/{videoId}.json`

Hotspot-Spark 后端只负责 **读材料 + Step3/Step4**，不再下载 MP4、不再做抖音提取重活。

## 已启用 R2 全局去重（跨天 / 跨机器共享）

规则（写死）：

- 全局缓存（复用源）：`ai-materials/douyin-by-id/{videoId}.json`
- 当天材料（Step3/Step4 读取用）：`ai-materials/YYYY/MM/DD/douyin/{videoId}.json`

命中全局缓存（`reuse_hit`）时：直接把全局 JSON 复制写入当天 key，并在 `meta` 里写入 `reused_from / reused_at / reuse_hit=true`，不会调用 `video_spider`、不会下载 MP4、不会调用 Gemini。

## 运行入口（Windows 任务计划程序友好）

脚本：`run-daily-newsource-douyin-local.ps1`

最简运行：

```powershell
cd E:\cursor\TrendRadar
$env:GEMINI_API_KEY = "<YOUR_GEMINI_KEY>"
.\run-daily-newsource-douyin-local.ps1
```

指定日期补跑：

```powershell
cd E:\cursor\TrendRadar
$env:GEMINI_API_KEY = "<YOUR_GEMINI_KEY>"
$env:DATE = "2026-01-01"
.\run-daily-newsource-douyin-local.ps1
```

强制忽略全局缓存并重新提取（会重新调用 `video_spider` / 下载 / Gemini；谨慎使用）：

```powershell
cd E:\cursor\TrendRadar
$env:GEMINI_API_KEY = "<YOUR_GEMINI_KEY>"
.\run-daily-newsource-douyin-local.ps1 -Force
```

## 依赖与配置

### 1) 本机服务

- `video_spider` 已启动（默认）：`http://127.0.0.1:8080`
  - `POST /analysis`（表单字段：`share_link`）

### 2) R2 / S3 环境变量

脚本会沿用 `run-daily-23-r2.ps1` 的解析方式，从项目里的 R2 信息 `.md` 自动解析并设置：

- `S3_ENDPOINT_URL`
- `S3_BUCKET_NAME`
- `S3_ACCESS_KEY_ID`
- `S3_SECRET_ACCESS_KEY`
- `S3_REGION`（默认 `auto`）

### 3) Gemini Key / Prompt

- `GEMINI_API_KEY`：必填（也兼容 `GOOGLE_API_KEY`）
- `GEMINI_MODEL`：默认 `gemini-3-flash-preview`
- `GEMINI_PROMPT_PATH`：默认 `E:\cursor\Hotspot-Spark\gemini提示词（提取画面+文案）.md`
  - 规则：提示词**必须整段原样使用**（不要精简/不要缩水），否则提取质量会下降；脚本不会改写该文件内容

### 4) Gemini 调用参数（写死）

- `temperature = 0.8`
- `maxOutputTokens = 30000`

### 4) 其它可选项

- `HOTSPARK_BASE`：默认 `https://hot-sparks.jacky.onl`
- `VIDEO_SPIDER_BASE`：默认 `http://127.0.0.1:8080`
- `CONCURRENCY`：默认 `2`（范围 `1~3`）
- `AI_MATERIALS_PREFIX`：默认 `ai-materials`
- `MAX_MP4_BYTES`：默认 `200MB`（防止异常大视频拖垮任务）

## 日志

- `output/newsource-douyin-local-YYYY-MM-DD.log`

每条会输出一行：`reuse_hit` / `reuse_miss`；任务结束会输出汇总：`reuse_hits / reuse_misses / gemini_calls`。

## 验收（跑完后怎么查）

材料覆盖率：

- `GET https://hot-sparks.jacky.onl/api/douyin/materials?date=YYYY-MM-DD`

抽查一条：

- `GET https://hot-sparks.jacky.onl/api/douyin/material?date=YYYY-MM-DD&id=videoId`
  - `doc.text / doc.visual / doc.why_hot` 应有值（失败则会有 `doc.meta.blocked_reason` 与 `doc.meta.error` 作为占位）
