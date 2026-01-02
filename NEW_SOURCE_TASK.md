# TrendRadar 新源定时任务（抖音增强材料）

目标：每天稳定产出抖音增强材料，写入 R2：`ai-materials/YYYY/MM/DD/douyin/{videoId}.json`，供 `Hotspot-Spark` 的 Step3/Step4 自动使用。

## 依赖与前提

- 本机已启动：`E:\cursor\video_spider`（默认监听 `http://127.0.0.1:8080`）
  - 接口：`POST /analysis`（表单字段：`share_link`）
- 线上 Hotspot-Spark（Cloudflare Pages）可访问：
  - 默认：`https://hot-sparks.jacky.onl`
- 你需要有 Hotspot-Spark 的入库鉴权：
  - 环境变量：`INGEST_TOKEN`（请求头 `X-INGEST-TOKEN`）

## 运行方式（Windows 任务计划程序友好）

脚本：`run-daily-newsource-r2.ps1`

最简运行（从 TrendRadar 目录）：

```powershell
$env:INGEST_TOKEN = "<YOUR_TOKEN>"
.\run-daily-newsource-r2.ps1
```

指定日期补跑：

```powershell
$env:INGEST_TOKEN = "<YOUR_TOKEN>"
$env:DATE = "2026-01-01"
.\run-daily-newsource-r2.ps1
```

常用可配置项（环境变量或同名参数均可）：

- `HOTSPARK_BASE`：默认 `https://hot-sparks.jacky.onl`
- `VIDEO_SPIDER_BASE`：默认 `http://127.0.0.1:8080`
- `INGEST_TOKEN`：必填（用于 `X-INGEST-TOKEN`）
- `CONCURRENCY`：解析并发（默认 2；范围 1~3）
- `DATE`：手动指定日期（YYYY-MM-DD）

运行日志输出：

- `output/newsource-YYYY-MM-DD.log`

## 工作流说明（脚本做什么）

1. 尝试读取 TrendRadar 的 R2 配置文件（沿用 `run-daily-23-r2.ps1` 的解析方式；该任务不依赖 R2 直连，读取失败也不会阻塞）
2. 计算日期（上海时区，YYYY-MM-DD）
3. 拉取当天抖音清单：
   - `GET /api/hotlist/douyin?date=DATE&refresh=1`
4. 对每条抖音链接调用本机解析器（`video_spider`）：
   - `POST http://127.0.0.1:8080/analysis`（`share_link=<douyin url or share text>`）
   - 使用返回的 `data.resource_path` 作为 `mp4_url`
   - 如返回的是图集（`resource_path` 为数组）会跳过该条（仅做视频增强）
   - 优先使用 `data.cover` 作为 `thumbnail`
5. 批量调用入库：
   - `POST /api/douyin/ingest`（一次传多条 `items`）

## 并发说明（重要）

脚本会在 PowerShell 7+ 且 `CONCURRENCY>1` 时启用并发解析；在 PowerShell 5.1 下会自动退化为串行（更稳）。

## 验收（本地跑完后怎么查）

跑完脚本后，打开：

- `https://hot-sparks.jacky.onl/api/douyin/materials?date=DATE`
  - `stats.materials_ready` 应逐步上升（后台任务异步执行，不要求立即全满）

抽查一条（把 `videoId` 换成实际 ID）：

- `https://hot-sparks.jacky.onl/api/douyin/material?date=DATE&id=videoId`
  - `doc` 里应能看到：`text`、`visual`、`why_hot`
