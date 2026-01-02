from __future__ import annotations

import argparse
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import boto3
import requests


COMMON_TRACKING_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "spm",
    "spm_id_from",
    "from",
    "source",
    "share",
    "share_token",
    "share_source",
    "timestamp",
    "tt_from",
    "channel",
    "wid",
    "spss",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today_iso_shanghai() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=8)).date().isoformat()


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


def normalize_url_for_dedupe(url: str) -> str:
    s = (url or "").strip()
    if not s:
        return ""
    try:
        parts = urlsplit(s)
    except Exception:
        return s.rstrip("/")

    scheme = (parts.scheme or "https").lower()
    netloc = (parts.netloc or "").lower()
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")

    pairs = []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        lk = (k or "").lower()
        if lk.startswith("utm_"):
            continue
        if lk in COMMON_TRACKING_KEYS:
            continue
        pairs.append((k, v))
    pairs.sort(key=lambda kv: (kv[0], kv[1]))
    query = urlencode(pairs, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def extract_url_from_line(rest: str) -> str | None:
    m = re.search(r"\[URL:([^\]]+)\]", rest or "")
    if not m:
        return None
    return m.group(1).strip() or None


def sanitize_title(s: str) -> str:
    t = (s or "").strip()
    t = re.sub(r"[\r\n\t]+", " ", t)
    t = re.sub(r"\s{2,}", " ", t)
    # Avoid breaking the simple bracket parser.
    return t.replace("[", "【").replace("]", "】").strip()


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


def tophub_extract_items(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return [it for it in data.get("items") if isinstance(it, dict)]
    if isinstance(data, list):
        return [it for it in data if isinstance(it, dict)]
    if isinstance(payload.get("items"), list):
        return [it for it in payload.get("items") if isinstance(it, dict)]
    return []


def fetch_tophub_node_urls(api_key: str, hashid: str, date_iso: str) -> list[dict[str, str]]:
    hk = (hashid or "").strip()
    ak = (api_key or "").strip()
    if not hk or not ak:
        return []

    base = "https://api.tophubdata.com"
    headers = {"Authorization": ak}

    items: list[dict[str, Any]] = []
    try:
        r = requests.get(f"{base}/nodes/{hk}/historys", params={"date": date_iso}, headers=headers, timeout=30)
        if r.status_code < 400:
            items = tophub_extract_items(r.json())
    except Exception:
        items = []

    if not items and date_iso == today_iso_shanghai():
        try:
            r = requests.get(f"{base}/nodes/{hk}", headers=headers, timeout=30)
            if r.status_code < 400:
                items = tophub_extract_items(r.json())
        except Exception:
            items = []

    out: list[dict[str, str]] = []
    for it in items:
        u = str(it.get("url") or "").strip()
        t = str(it.get("title") or "").strip()
        if not u or not t:
            continue
        out.append({"title": sanitize_title(t), "url": u})
    return out


def merge_hotspots_text(
    original: str,
    new_items: list[dict[str, str]],
    *,
    platform_name: str,
    limit: int,
    keep_total: int,
) -> str:
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
                header.append(ln)

    original_total = len(hotspot_rests)
    if keep_total <= 0:
        keep_total = original_total

    # Existing URLs for de-dup.
    seen_norm: set[str] = set()
    for rest in hotspot_rests:
        u = extract_url_from_line(rest) or ""
        nu = normalize_url_for_dedupe(u)
        if nu:
            seen_norm.add(nu)

    # Build new rest lines (no numbering yet), de-duped.
    new_rests: list[str] = []
    rank = 1
    for it in new_items[: max(0, int(limit or 0))]:
        title = sanitize_title(str(it.get("title") or "").strip())
        url = str(it.get("url") or "").strip()
        if not title or not url:
            continue
        nu = normalize_url_for_dedupe(url)
        if nu and nu in seen_norm:
            continue
        if nu:
            seen_norm.add(nu)
        new_rests.append(f"[platform={platform_name}] {title} [URL:{url}] [RANK:{rank}]")
        rank += 1

    if not new_rests:
        return original

    # Put new items at the top to ensure they participate in Step3/4,
    # then keep original order for the rest, and trim to keep_total.
    out_rests = new_rests + hotspot_rests

    # Remove duplicates (by normalized URL) while preserving first occurrence.
    seen2: set[str] = set()
    uniq: list[str] = []
    for rest in out_rests:
        u = extract_url_from_line(rest) or ""
        nu = normalize_url_for_dedupe(u)
        if nu and nu in seen2:
            continue
        if nu:
            seen2.add(nu)
        uniq.append(rest)

    uniq = uniq[:keep_total]

    numbered = [f"{i}. {rest}".rstrip() for i, rest in enumerate(uniq, start=1)]
    return "\n".join([x for x in header if x is not None] + numbered).rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--prefix", default="ai-hotspots")
    ap.add_argument("--filename", default="hotspots.txt")
    ap.add_argument("--hashid", default=os.getenv("TOPHUB_NETEASE_HASHID") or "ENeYa4DeY4")
    ap.add_argument("--platform", default="网易新闻")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--keep-total", type=int, default=0, help="keep hotspots count (0 = keep current)")
    ap.add_argument("--api-key", default=os.getenv("TOPHUB_API_KEY") or "")
    args = ap.parse_args()

    date_iso = args.date.strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_iso):
        raise SystemExit("Invalid --date, expected YYYY-MM-DD")

    api_key = (args.api_key or "").strip()
    if not api_key:
        raise SystemExit("Missing TOPHUB_API_KEY (pass --api-key or set env var).")

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

    items = fetch_tophub_node_urls(api_key=api_key, hashid=str(args.hashid), date_iso=date_iso)
    if not items:
        print(f"skip date={date_iso} key={key} reason=no_items at={now_iso()}")
        return 0

    merged = merge_hotspots_text(
        original,
        items,
        platform_name=str(args.platform),
        limit=int(args.limit),
        keep_total=int(args.keep_total),
    )
    r2.put_text(key, merged)
    print(f"ok date={date_iso} key={key} new_netease={len(items[: int(args.limit)])} at={now_iso()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

