# coding=utf-8
"""
把本地历史输出补齐为“AI 热点原料”并上传到 R2。

支持两类来源：
1) 新版本地 SQLite：output/YYYY-MM-DD/news.db（可做“相对昨天去重”）
2) 旧版 TXT 目录：output/YYYY年MM月DD日/txt/*.txt（按最后一份快照转成统一格式）

R2 Key 规则：{prefix}/YYYY/MM/DD/hotspots.txt
"""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from trendradar.ai_hotspots import (
    AiHotspotLine,
    build_daily_unique_hotspots,
    build_r2_key,
    render_ai_hotspots_text,
    write_ai_hotspots_file,
)
from trendradar.core import load_config
from trendradar.report.helpers import clean_title
from trendradar.storage.manager import get_storage_manager
from trendradar.storage.remote import RemoteStorageBackend
from trendradar.utils.time import get_configured_time
from trendradar.utils.url import normalize_url


@dataclass(frozen=True)
class _LegacyItem:
    platform_id: str
    platform_name: str
    rank: int
    text: str  # 已包含 URL/MOBILE 等标签（如有）


def _parse_iso_date_folder(name: str) -> Optional[str]:
    if re.match(r"^\d{4}-\d{2}-\d{2}$", name):
        return name
    return None


def _parse_cn_date_folder(name: str) -> Optional[str]:
    m = re.match(r"^(\d{4})年(\d{2})月(\d{2})日$", name)
    if not m:
        return None
    y, mo, d = m.group(1), m.group(2), m.group(3)
    return f"{y}-{mo}-{d}"


def _iter_date_folders(output_dir: Path) -> List[Tuple[str, Path, str]]:
    """
    Returns: [(date_str, folder_path, kind)]
    kind: "db" | "legacy_txt"
    """
    items: List[Tuple[str, Path, str]] = []
    for p in sorted(output_dir.iterdir()):
        if not p.is_dir():
            continue
        if p.name.startswith("."):
            continue

        iso = _parse_iso_date_folder(p.name)
        if iso:
            db_path = p / "news.db"
            if db_path.exists():
                items.append((iso, p, "db"))
            continue

        cn = _parse_cn_date_folder(p.name)
        if cn:
            txt_dir = p / "txt"
            if txt_dir.exists() and txt_dir.is_dir():
                items.append((cn, p, "legacy_txt"))
            continue

    return items


def _pick_latest_txt_snapshot(folder: Path) -> Optional[Path]:
    txt_dir = folder / "txt"
    if not txt_dir.exists():
        return None
    candidates = [p for p in txt_dir.iterdir() if p.is_file() and p.suffix.lower() == ".txt"]
    if not candidates:
        return None
    return sorted(candidates, key=lambda x: x.stat().st_mtime, reverse=True)[0]


def _legacy_identity(platform_id: str, text: str) -> str:
    # 优先 URL
    m = re.search(r"\[URL:(.+?)\]", text)
    if m:
        return f"url:{normalize_url(m.group(1).strip(), platform_id)}"
    return f"title:{clean_title(text).lower()}"


def _parse_legacy_txt_snapshot(path: Path) -> List[_LegacyItem]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]

    current_platform_id = ""
    current_platform_name = ""
    items: List[_LegacyItem] = []

    header_re = re.compile(r"^([a-zA-Z0-9_-]+)\s*\|\s*(.+)$")
    item_re = re.compile(r"^(\d+)\.\s+(.+)$")

    for ln in lines:
        hm = header_re.match(ln)
        if hm:
            current_platform_id = hm.group(1).strip()
            current_platform_name = hm.group(2).strip()
            continue

        im = item_re.match(ln)
        if im and current_platform_id:
            rank = int(im.group(1))
            text = im.group(2).strip()
            items.append(
                _LegacyItem(
                    platform_id=current_platform_id,
                    platform_name=current_platform_name or current_platform_id,
                    rank=rank,
                    text=text,
                )
            )
            continue

    # 同一天同一份快照里做一次去重（避免不同平台/重复抓取导致的重复项）
    seen = set()
    deduped: List[_LegacyItem] = []
    for it in items:
        ident = _legacy_identity(it.platform_id, it.text)
        if ident in seen:
            continue
        seen.add(ident)
        deduped.append(it)
    return deduped


def _render_legacy_as_ai_hotspots(
    *,
    date_str: str,
    generated_at: datetime,
    snapshot_path: Path,
    legacy_items: List[_LegacyItem],
) -> str:
    header = [
        "# TrendRadar 热点原料（AI可读）",
        f"- date: {date_str}",
        f"- generated_at: {generated_at.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- source: legacy_txt_snapshot ({snapshot_path.name})",
        "",
        "说明：下面每条前面的数字序号，就是【来源ID】（source_id）。后续 Step3/Step4 必须引用这个序号，程序才能回填平台与URL。",
        "",
    ]

    body: List[str] = []
    for idx, it in enumerate(legacy_items, start=1):
        # legacy text 本身可能已包含 [URL:...] 等标签；这里保持原样，只补齐平台信息
        line = f"{idx}. [platform={it.platform_name}] [platform_id={it.platform_id}] {clean_title(it.text)}"
        if it.rank:
            line += f" [RANK:{it.rank}]"
        body.append(line)

    return "\n".join(header + body) + "\n"


def _upload_text_to_r2(*, config: dict, key: str, content: str) -> None:
    remote_cfg = (config.get("STORAGE") or {}).get("REMOTE") or {}
    if not (
        remote_cfg.get("BUCKET_NAME")
        and remote_cfg.get("ACCESS_KEY_ID")
        and remote_cfg.get("SECRET_ACCESS_KEY")
        and remote_cfg.get("ENDPOINT_URL")
    ):
        raise RuntimeError("missing remote storage config (S3_* env vars or storage.remote)")

    remote = RemoteStorageBackend(
        bucket_name=remote_cfg.get("BUCKET_NAME", ""),
        access_key_id=remote_cfg.get("ACCESS_KEY_ID", ""),
        secret_access_key=remote_cfg.get("SECRET_ACCESS_KEY", ""),
        endpoint_url=remote_cfg.get("ENDPOINT_URL", ""),
        region=remote_cfg.get("REGION", ""),
        enable_txt=False,
        enable_html=False,
        timezone=config.get("TIMEZONE", "Asia/Shanghai"),
    )

    ok = remote.upload_text_object(key, content)
    if not ok:
        raise RuntimeError(f"upload failed: {key}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--local-ai-dir", default="output/ai_hotspots")
    parser.add_argument("--prefix", default="", help="R2 prefix (default from config ai_export.r2.prefix)")
    parser.add_argument("--filename", default="", help="filename (default from config ai_export.r2.filename)")
    parser.add_argument("--max-items", type=int, default=300)
    parser.add_argument("--dedupe-days", type=int, default=1)
    parser.add_argument("--from-date", default="", help="YYYY-MM-DD (inclusive)")
    parser.add_argument("--to-date", default="", help="YYYY-MM-DD (inclusive)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    # 避免 Windows 控制台编码影响（尽量不输出表情/特殊符号）
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    config = load_config()
    tz = config.get("TIMEZONE", "Asia/Shanghai")
    now = get_configured_time(tz)

    ai_cfg = config.get("AI_EXPORT", {}) or {}
    prefix = args.prefix or (ai_cfg.get("R2") or {}).get("PREFIX") or "ai-hotspots"
    filename = args.filename or (ai_cfg.get("R2") or {}).get("FILENAME") or "hotspots.txt"

    output_dir = Path(args.output_dir)
    local_ai_dir = args.local_ai_dir
    max_items = int(args.max_items or 0)
    dedupe_days = int(args.dedupe_days or 0)

    from_dt = datetime.min
    to_dt = datetime.max
    if args.from_date:
        from_dt = datetime.strptime(args.from_date, "%Y-%m-%d")
    if args.to_date:
        to_dt = datetime.strptime(args.to_date, "%Y-%m-%d")

    # 用 local backend 读历史 db
    storage_manager = get_storage_manager(
        backend_type="local",
        data_dir=str(output_dir),
        enable_txt=False,
        enable_html=False,
        timezone=tz,
        force_new=True,
    )

    candidates = _iter_date_folders(output_dir)
    filtered: List[Tuple[str, Path, str]] = []
    for date_str, folder, kind in candidates:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        if dt < from_dt or dt > to_dt:
            continue
        filtered.append((date_str, folder, kind))

    if not filtered:
        print("no date folders to backfill")
        return 0

    uploaded = 0
    for date_str, folder, kind in filtered:
        content = ""
        generated_at = now
        dedupe_against_date: Optional[str] = None

        if kind == "db":
            today_data = storage_manager.get_latest_crawl_data(date_str)
            if today_data is None:
                continue

            yesterday_data = None
            if dedupe_days > 0:
                dedupe_dt = datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=dedupe_days)
                dedupe_against_date = dedupe_dt.strftime("%Y-%m-%d")
                try:
                    yesterday_data = storage_manager.get_latest_crawl_data(dedupe_against_date)
                except Exception:
                    yesterday_data = None

            lines, total_candidates = build_daily_unique_hotspots(
                today_data=today_data,
                yesterday_data=yesterday_data,
                max_items=max_items,
            )

            content = render_ai_hotspots_text(
                lines=lines,
                date_str=date_str,
                generated_at=generated_at,
                dedupe_against_date=dedupe_against_date,
                total_candidates=total_candidates,
            )

        elif kind == "legacy_txt":
            snapshot = _pick_latest_txt_snapshot(folder)
            if snapshot is None:
                continue

            legacy_items = _parse_legacy_txt_snapshot(snapshot)
            if max_items > 0:
                legacy_items = legacy_items[:max_items]

            content = _render_legacy_as_ai_hotspots(
                date_str=date_str,
                generated_at=generated_at,
                snapshot_path=snapshot,
                legacy_items=legacy_items,
            )
        else:
            continue

        if not content.strip():
            continue

        local_path = write_ai_hotspots_file(
            local_base_dir=local_ai_dir,
            date_str=date_str,
            filename=filename,
            content=content,
        )

        key = build_r2_key(prefix, date_str, filename)

        print(f"date={date_str} kind={kind} local={local_path} r2={key}")
        if args.dry_run:
            continue

        _upload_text_to_r2(config=config, key=key, content=content)
        uploaded += 1

    print(f"done. uploaded={uploaded}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

