from __future__ import annotations

import argparse
import os
import re
from datetime import datetime, timezone
from typing import Any

import boto3
import requests


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_hotspots_key(prefix: str, date_iso: str, filename: str) -> str:
    y, m, d = date_iso.split("-", 3)
    prefix = (prefix or "").strip().strip("/")
    return f"{prefix}/{y}/{m}/{d}/{filename}"

def build_hotspots_key_candidates(prefix: str, date_iso: str, filename: str) -> list[str]:
    y, m_raw, d_raw = date_iso.split("-", 3)
    try:
        m_num = int(m_raw)
        d_num = int(d_raw)
    except Exception:
        return [build_hotspots_key(prefix, date_iso, filename)]

    padded = build_hotspots_key(prefix, date_iso, filename)
    unpadded = f"{(prefix or '').strip().strip('/')}/{y}/{m_num}/{d_num}/{filename}"
    return [padded] if padded == unpadded else [padded, unpadded]


def is_numbered_line(line: str) -> bool:
    return bool(re.match(r"^\d+\.\s+", line or ""))


def strip_number_prefix(line: str) -> str:
    return re.sub(r"^\d+\.\s+", "", line or "").strip()


def extract_url_from_line(rest: str) -> str | None:
    m = re.search(r"\[URL:([^\]]+)\]", rest or "")
    if not m:
        return None
    return m.group(1).strip() or None


def is_douyin_url(url: str) -> bool:
    u = (url or "").lower()
    return ("douyin.com" in u) or ("v.douyin.com" in u) or ("iesdouyin.com" in u)


def is_douyin_hotspot_rest(rest: str) -> bool:
    url = extract_url_from_line(rest) or ""
    return bool(url and is_douyin_url(url))


class R2:
    def __init__(self) -> None:
        endpoint = (os.getenv("S3_ENDPOINT_URL") or "").strip()
        bucket = (os.getenv("S3_BUCKET_NAME") or "").strip()
        ak = (os.getenv("S3_ACCESS_KEY_ID") or "").strip()
        sk = (os.getenv("S3_SECRET_ACCESS_KEY") or "").strip()
        region = (os.getenv("S3_REGION") or "auto").strip()

        if not endpoint or not bucket or not ak or not sk:
            raise RuntimeError("Missing R2 env vars: S3_ENDPOINT_URL/S3_BUCKET_NAME/S3_ACCESS_KEY_ID/S3_SECRET_ACCESS_KEY")

        self.bucket = bucket
        self.s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=ak,
            aws_secret_access_key=sk,
            region_name=region,
        )

    def get_text(self, key: str) -> str | None:
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=key)
            body = obj["Body"].read()
            return body.decode("utf-8", errors="replace")
        except Exception:
            return None

    def put_text(self, key: str, text: str) -> None:
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=text.encode("utf-8"),
            ContentType="text/plain; charset=utf-8",
        )


def fetch_new_douyin_items(hotspot_base: str, date_iso: str) -> list[dict[str, Any]]:
    base = (hotspot_base or "").strip().rstrip("/")
    if not base:
        base = "https://hot-sparks.jacky.onl"

    url = f"{base}/api/hotlist/douyin?date={date_iso}&refresh=1"
    data = requests.get(url, timeout=30).json()
    items = data.get("items") if isinstance(data, dict) else None

    # If history is empty for today, allow fallback to "latest".
    if not isinstance(items, list) or not items:
        url2 = f"{base}/api/hotlist/douyin?source=latest&refresh=1"
        data2 = requests.get(url2, timeout=30).json()
        items2 = data2.get("items") if isinstance(data2, dict) else None
        items = items2 if isinstance(items2, list) else []

    out: list[dict[str, Any]] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()
        u = str(it.get("url") or "").strip()
        if not title or not u:
            continue
        out.append({"title": title, "url": u})
    return out


def merge_hotspots_text(original: str, new_douyin: list[dict[str, str]]) -> str:
    lines = original.splitlines()

    header: list[str] = []
    hotspot_rests: list[str] = []

    seen_hotspot = False
    for ln in lines:
        if is_numbered_line(ln):
            seen_hotspot = True
            hotspot_rests.append(strip_number_prefix(ln))
        else:
            if not seen_hotspot:
                header.append(ln)
            else:
                # Keep any trailing non-numbered lines as-is (rare, but safe).
                header.append(ln)

    # Build new douyin rest lines (no numbering yet).
    new_douyin_rests: list[str] = []
    for idx, it in enumerate(new_douyin, start=1):
        title = str(it.get("title") or "").strip()
        url = str(it.get("url") or "").strip()
        if not title or not url:
            continue
        # Keep format consistent with Hotspot-Spark parser.
        new_douyin_rests.append(f"[platform=抖音] {title} [URL:{url}] [RANK:{idx}]")

    # Replace old douyin slots first; if fewer new items than removed, remaining slots are removed.
    out_rests: list[str] = []
    cursor = 0
    for rest in hotspot_rests:
        if is_douyin_hotspot_rest(rest):
            if cursor < len(new_douyin_rests):
                out_rests.append(new_douyin_rests[cursor])
                cursor += 1
            else:
                continue
        else:
            out_rests.append(rest)

    # Append remaining new items.
    while cursor < len(new_douyin_rests):
        out_rests.append(new_douyin_rests[cursor])
        cursor += 1

    # Renumber sequentially.
    numbered = [f"{i}. {rest}".rstrip() for i, rest in enumerate(out_rests, start=1)]

    # Preserve original newline style loosely; always end with newline.
    combined = "\n".join([x for x in header if x is not None] + numbered).rstrip() + "\n"
    return combined


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--hotspot-base", default="https://hot-sparks.jacky.onl")
    ap.add_argument("--prefix", default="ai-hotspots")
    ap.add_argument("--filename", default="hotspots.txt")
    args = ap.parse_args()

    date_iso = args.date.strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_iso):
        raise SystemExit("Invalid --date, expected YYYY-MM-DD")

    r2 = R2()
    key = ""
    original: str | None = None
    for k in build_hotspots_key_candidates(args.prefix, date_iso, args.filename):
        original = r2.get_text(k)
        if original:
            key = k
            break
    if not original or not key:
        raise SystemExit(f"Missing hotspots in R2 for date={date_iso}")

    new_items = fetch_new_douyin_items(args.hotspot_base, date_iso)
    if not new_items:
        raise SystemExit("No new douyin items fetched; aborting to avoid deleting old entries.")

    merged = merge_hotspots_text(original, new_items)
    r2.put_text(key, merged)
    print(f"ok date={date_iso} key={key} new_douyin={len(new_items)} at={now_iso()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
