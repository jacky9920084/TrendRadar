# TopHub Data API（TrendRadar 用法备忘）

用途：从 TopHub Data 拉取各平台热榜（本项目主要用抖音/头条等），作为“新源热点清单”的来源。

官方文档：`https://www.tophubdata.com/documentation`

## 鉴权

- 请求头：`Authorization: <ACCESS_KEY>`
- 推荐通过环境变量配置：`TOPHUB_API_KEY`

建议：团队协作时不要把 key 写进仓库；在本机用环境变量或本地文件（不提交）配置即可。

## 本项目用到的核心接口

### 1) 获取“某天历史榜单”（优先）

`GET https://api.tophubdata.com/nodes/{hashid}/historys?date=YYYY-MM-DD`

- 注意：该接口有时会出现“指定日期返回空数组”的情况（可能是数据归档未生成/时区/平台更新节奏等），需要兜底。

### 2) 获取“实时榜单”（兜底）

`GET https://api.tophubdata.com/nodes/{hashid}`

当历史为空时，用实时榜单保证链路不断。

## hashid（项目当前关心的平台）

- 抖音：`DpQvNABoNE`
- 网易新闻实时热榜：`ENeYa4DeY4`
-（如后续要加头条/微信/豆瓣/知乎等，建议在代码/配置里集中维护一份映射表）

## 调用示例（PowerShell）

```powershell
$apiKey = $env:TOPHUB_API_KEY
$hashid = 'DpQvNABoNE'
$date = '2026-01-02'

# 历史（优先）
$his = Invoke-RestMethod -Uri "https://api.tophubdata.com/nodes/$hashid/historys?date=$date" -Headers @{ Authorization = $apiKey }

# 实时（兜底）
$latest = Invoke-RestMethod -Uri "https://api.tophubdata.com/nodes/$hashid" -Headers @{ Authorization = $apiKey }
```

## 与 Hotspot-Spark 的关系（推荐做法）

如果走 Hotspot-Spark 统一拉取与缓存：

- `GET https://hot-sparks.jacky.onl/api/hotlist/douyin?date=YYYY-MM-DD`
  - 内部会先尝试 TopHub 历史；空则自动兜底实时
  - 响应字段 `source` 会说明最终用的是历史还是实时（便于排查）
