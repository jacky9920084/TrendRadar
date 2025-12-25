# coding=utf-8
"""
AI 热点导出（给下游大模型用的“干净文本原料”）

目标：
- 每天导出一份“当天新增热点（相对昨天不重复）”列表
- 格式可读、可索引（序号就是 source_id）
- 平台/URL 等元信息由程序保留，避免下游模型重复生成浪费 token

该模块不负责调用大模型，只负责把 TrendRadar 的抓取结果变成“可喂给 Step3 的文本”。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from trendradar.report.helpers import clean_title
from trendradar.storage.base import NewsData, NewsItem
from trendradar.utils.time import get_configured_time
from trendradar.utils.url import normalize_url


@dataclass(frozen=True)
class AiHotspotLine:
    """
    单条热点的导出模型（用于渲染到文本）
    """

    idx: int
    platform_id: str
    platform_name: str
    title: str
    url: str = ""
    mobile_url: str = ""
    rank: int = 0


def _item_identity(item: NewsItem) -> str:
    """
    用于“跨天去重”的稳定标识：
    - 优先 URL（标准化后）
    - 无 URL 时退化到 title（清洗后小写）
    """
    url = (item.url or "").strip()
    if url:
        return f"url:{normalize_url(url, item.source_id)}"
    return f"title:{clean_title(item.title).lower()}"


def _flatten_news(
    data: NewsData,
    id_to_name: Dict[str, str],
) -> List[NewsItem]:
    items: List[NewsItem] = []
    for source_id, news_list in (data.items or {}).items():
        source_name = id_to_name.get(source_id) or data.id_to_name.get(source_id) or source_id
        for it in news_list:
            # 填充 source_name（数据库不存）
            it.source_name = source_name
            items.append(it)

    items.sort(key=lambda x: (x.rank or 999, x.source_id or "", clean_title(x.title)))
    return items


def build_daily_unique_hotspots(
    *,
    today_data: NewsData,
    yesterday_data: Optional[NewsData],
    max_items: int,
) -> Tuple[List[AiHotspotLine], int]:
    """
    生成“当天新增热点（相对昨天不重复）”列表。

    Returns:
        (lines, total_candidates)
        - lines: 最终输出的热点行（已按 idx 重新编号）
        - total_candidates: 去重前（过滤 yesterday 后）的总条数（未截断）
    """
    today_items = _flatten_news(today_data, today_data.id_to_name)

    yesterday_seen: Set[str] = set()
    if yesterday_data is not None:
        for it in _flatten_news(yesterday_data, yesterday_data.id_to_name):
            yesterday_seen.add(_item_identity(it))

    # 先“跨天去重”，再“当天内去重”
    unique_candidates: List[NewsItem] = []
    today_seen: Set[str] = set()
    for it in today_items:
        identity = _item_identity(it)
        if identity in yesterday_seen:
            continue
        if identity in today_seen:
            continue
        today_seen.add(identity)
        unique_candidates.append(it)

    total_candidates = len(unique_candidates)
    if max_items > 0:
        unique_candidates = unique_candidates[:max_items]

    lines: List[AiHotspotLine] = []
    for idx, it in enumerate(unique_candidates, start=1):
        lines.append(
            AiHotspotLine(
                idx=idx,
                platform_id=it.source_id,
                platform_name=it.source_name or it.source_id,
                title=clean_title(it.title),
                url=(it.url or "").strip(),
                mobile_url=(it.mobile_url or "").strip(),
                rank=int(it.rank or 0),
            )
        )

    return lines, total_candidates


def render_ai_hotspots_text(
    *,
    lines: List[AiHotspotLine],
    date_str: str,
    generated_at: datetime,
    dedupe_against_date: Optional[str],
    total_candidates: int,
) -> str:
    """
    渲染为“给 Step3 喂的文本”，重点是可读+可索引。
    """
    header = [
        f"# TrendRadar 热点原料（AI可读）",
        f"- date: {date_str}",
        f"- generated_at: {generated_at.strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    if dedupe_against_date:
        header.append(f"- dedupe_against: {dedupe_against_date}")
    header.extend(
        [
            f"- candidates_after_dedupe: {total_candidates}",
            f"- exported_count: {len(lines)}",
            "",
            "说明：下面每条前面的数字序号，就是【来源ID】（source_id）。后续 Step3/Step4 必须引用这个序号，程序才能回填平台与URL。",
            "",
        ]
    )

    body: List[str] = []
    for it in lines:
        line = f"{it.idx}. [platform={it.platform_name}] [platform_id={it.platform_id}] {it.title}"
        if it.url:
            line += f" [URL:{it.url}]"
        if it.mobile_url:
            line += f" [MOBILE:{it.mobile_url}]"
        if it.rank:
            line += f" [RANK:{it.rank}]"
        body.append(line)

    return "\n".join(header + body) + "\n"


def write_ai_hotspots_file(
    *,
    local_base_dir: str,
    date_str: str,
    filename: str,
    content: str,
) -> str:
    """
    写入本地文件：local_base_dir/YYYY/MM/DD/filename
    """
    y, m, d = date_str.split("-", 2)
    out_dir = Path(local_base_dir) / y / m / d
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / filename
    out_path.write_text(content, encoding="utf-8")
    return str(out_path)


def build_r2_key(prefix: str, date_str: str, filename: str) -> str:
    """
    构造 R2 Key：{prefix}/YYYY/MM/DD/{filename}
    """
    prefix = (prefix or "").strip().strip("/")
    y, m, d = date_str.split("-", 2)
    parts = [p for p in [prefix, y, m, d, filename] if p]
    return "/".join(parts)

