# 热点选题流水线（本机自动化 + Cloudflare 轻后端）

本仓库承担“本机重活”的部分：每天生成热点主清单，并对抖音/网页做材料增强，写入 Cloudflare R2。

后端与前端展示由 `Hotspot-Spark` 仓库负责（Cloudflare Pages Functions + UI）。

## 组件与职责

- TrendRadar（本机）：生成 `hotspots.txt`；抖音增强（本机下载临时 MP4 → Gemini 提取）；网页增强（白名单抽正文）；最后把结果写入 R2。
- Hotspot-Spark（Cloudflare）：读取 R2 的主清单与增强材料，拼接后调用 Step3/Step4（Gemini）做选题分析，并把结果写回 R2。
- video_spider（本机服务）：解析抖音分享链接/长链，得到可下载 `mp4_url`，供抖音增强链路使用。

## 必备配置（不要提交到仓库）

1) R2 访问（S3 兼容）

创建 `config\r2_info.local.md`（格式同 `config\r2_info.example.md`），脚本会自动读取并设置：

- `S3_ENDPOINT_URL`
- `S3_BUCKET_NAME`
- `S3_ACCESS_KEY_ID`
- `S3_SECRET_ACCESS_KEY`
- `S3_REGION=auto`

2) TopHub（用于网易热榜合并，及部分白名单补充来源）

- 环境变量：`TOPHUB_API_KEY`
  - 或创建 `config\tophub_api_key.local.txt`（内容就是 key，格式同 `config\tophub_api_key.example.txt`）

3) Gemini（抖音增强用）

- 环境变量：`GEMINI_API_KEY`（或 `GOOGLE_API_KEY`）
- 提示词文件：`E:\cursor\Hotspot-Spark\gemini提示词（提取画面+文案）.md`

## 在另一台电脑首次安装（建议照做）

1) 拉取仓库

```powershell
git clone https://github.com/jacky9920084/TrendRadar.git
cd TrendRadar
```

2) 准备 Python 虚拟环境与依赖

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

3) 准备本机配置（不要提交）

- `config\r2_info.local.md`（见 `config\r2_info.example.md`）
- `config\tophub_api_key.local.txt`（可选；仅网易合并需要；见 `config\tophub_api_key.example.txt`）
- 环境变量 `GEMINI_API_KEY`

4) 启动 `video_spider`（抖音增强必需）

- 推荐方式：单独把 `E:\cursor\video_spider` 作为第三个仓库维护并在本机启动服务
- 目标：保证 `http://127.0.0.1:8080/analysis` 可用

如果你不想再单独维护 `video_spider` 仓库，本仓库也内置了一个可直接运行的二进制（Windows）：

- `E:\cursor\TrendRadar\tools\video_spider\analysis.exe`

运行方式（建议单独开一个 PowerShell 窗口常驻）：

```powershell
cd E:\cursor\TrendRadar\tools\video_spider
.\analysis.exe
```

## 本机一键跑（推荐）

每天跑一次（会写日志到 `output\`）：

```powershell
cd E:\cursor\TrendRadar
.\run-daily-master.ps1
```

补跑指定日期：

```powershell
cd E:\cursor\TrendRadar
.\run-daily-master.ps1 -Date 2026-01-02
```

## 产出（写入 R2 的 Key 约定）

- 主清单：`ai-hotspots/YYYY/MM/DD/hotspots.txt`
- 抖音材料：`ai-materials/YYYY/MM/DD/douyin/<videoId>.json`
- 网页材料：`ai-materials/YYYY/MM/DD/web/<sha1(normalized_url)>.json`

## video_spider（本机服务）

本机需要先启动 `video_spider`（默认端口 `8080`）：

```bash
curl --location --request POST "http://127.0.0.1:8080/analysis" ^
  --header "Content-Type: application/x-www-form-urlencoded" ^
  --data-urlencode "share_link=https://www.douyin.com/video/<id>"
```

> 如果 `video_spider` 未启动，`run-daily-master.ps1` 会跳过抖音增强步骤，但主清单仍会生成上传。
