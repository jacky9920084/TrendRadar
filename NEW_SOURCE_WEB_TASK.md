# TrendRadar 新源定时任务（网页正文增强：头条/微信/豆瓣/知乎/网易新闻）

目标：从 R2 的主清单 `ai-hotspots/YYYY/MM/DD/hotspots.txt` 中筛出网页类 URL（以及可选的 TopHub「网易新闻实时热榜」来源），抓取正文并写入：

- `ai-materials/YYYY/MM/DD/web/{sha1}.json`

Hotspot-Spark 会读取这些材料，把 `excerpt/clean_text` 拼进 Step3/Step4 的输入（不再只靠标题+链接）。

## 运行入口（Windows 任务计划程序友好）

脚本：`run-daily-newsource-web.ps1`

最简运行：

```powershell
cd E:\cursor\TrendRadar
.\run-daily-newsource-web.ps1
```

指定日期补跑：

```powershell
cd E:\cursor\TrendRadar
$env:DATE = "2026-01-01"
.\run-daily-newsource-web.ps1
```

强制覆盖当天已有材料（默认存在且正常会跳过）：

```powershell
cd E:\cursor\TrendRadar
.\run-daily-newsource-web.ps1 -Force
```

## 依赖与配置

1) 需要 R2/S3 环境变量（脚本会从项目里的 R2 信息 `.md` 自动解析并设置）：

- `S3_ENDPOINT_URL`
- `S3_BUCKET_NAME`
- `S3_ACCESS_KEY_ID`
- `S3_SECRET_ACCESS_KEY`
- `S3_REGION`（默认 `auto`）

2) Python 依赖：

- 必须：`requests`、`boto3`
- 可选：`trafilatura`（用于更稳的正文抽取；未安装会自动降级为简单抽取）

说明：脚本会调用同目录下的 `newsource_web.py`（独立脚本，不依赖 TrendRadar 包导入）。

补充：若需要从 TopHub 补充“网易新闻实时热榜”，需配置 `TOPHUB_API_KEY`（脚本默认已内置你提供的 key；也可自行覆盖环境变量）。

## 产出 schema（稳定字段）

对象键：`ai-materials/YYYY/MM/DD/web/{sha1}.json`，内容示例字段：

- `title`：页面标题（优先页面真实 title）
- `url`：原链接
- `site`：`toutiao|weixin|douban|zhihu|netease|other`
- `canonical_url`：能拿到就写
- `fetched_at`：抓取时间（ISO）
- `excerpt`：摘要（200~500字）
- `clean_text`：正文纯文本（最多约 6000 字；超出截断）
- `meta.blocked_reason`：失败/遮罩原因（为空表示可用）

## 日志

- `output/newsource-web-YYYY-MM-DD.log`

包含：R2 key、总 URL 数、成功/跳过/失败统计、失败原因 TopN（以 Python summary JSON 形式写入）。
