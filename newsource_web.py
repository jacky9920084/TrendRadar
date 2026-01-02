# coding=utf-8
"""
Web content enhancement task (standalone script):
- Download daily hotspots.txt from R2
- Extract web article content (excerpt + optional clean_text)
- Upload per-URL materials to R2: ai-materials/YYYY/MM/DD/web/{sha1}.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit, unquote_plus

import requests

try:
    import boto3
    from botocore.config import Config as BotoConfig
    from botocore.exceptions import ClientError

    HAS_BOTO3 = True
except Exception:
    boto3 = None
    BotoConfig = None
    ClientError = Exception
    HAS_BOTO3 = False


COMMON_TRACKING_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "ref",
    "referrer",
    "source",
    "channel",
    "_t",
    "timestamp",
    "_",
    "random",
    "share_token",
    "share_id",
    "share_from",
    "spm",
    "from",
    "tt_from",
    "vd_source",
}


def now_iso() -> str:
    # Python 3.13: avoid deprecated utcnow(); always generate explicit UTC timestamp.
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def assert_iso_date(date_str: str) -> None:
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str or ""):
        raise ValueError(f"Invalid date format: {date_str} (expected YYYY-MM-DD).")


def normalize_url_for_hash(url: str) -> str:
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


def sha1_hex(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


def detect_site(url: str) -> str:
    try:
        host = (urlsplit(url).netloc or "").lower()
    except Exception:
        host = ""
    if "toutiao.com" in host:
        return "toutiao"
    if host.endswith("weixin.qq.com") or "weixin.qq.com" in host:
        return "weixin"
    if "douban.com" in host:
        return "douban"
    if "zhihu.com" in host:
        return "zhihu"
    if "163.com" in host:
        return "netease"
    return "other"

def extract_toutiao_keyword(url: str, title_hint: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return (title_hint or "").strip()
    try:
        p = urlsplit(raw)
    except Exception:
        return (title_hint or "").strip()

    # so.toutiao.com/search?keyword=...
    keyword = ""
    for k, v in parse_qsl(p.query, keep_blank_values=True):
        if (k or "").lower() == "keyword":
            keyword = v
            break
    keyword = unquote_plus(keyword or "").strip()

    if keyword:
        # Often looks like: "#跨年人群...#322.6万" or "跨年人群..."
        keyword = keyword.strip()
        keyword = keyword.strip("#").strip()
        # Remove trailing heat numbers if present.
        keyword = re.sub(r"#?\s*\d+(\.\d+)?\s*(万|w)?\s*$", "", keyword).strip()
        keyword = keyword.strip("#").strip()

    return keyword or (title_hint or "").strip()


def fetch_toutiao_search_snippet(session: requests.Session, keyword: str) -> Tuple[str, str]:
    """
    Try to turn a Toutiao search keyword into a usable snippet without rendering JS.
    Returns (excerpt, clean_text). Empty strings on failure.
    """
    kw = (keyword or "").strip()
    if not kw:
        return "", ""

    # This endpoint is widely used by Toutiao web search; may still be rate-limited/blocked sometimes.
    api = "https://www.toutiao.com/api/search/content/"
    params = {
        "keyword": kw,
        "offset": 0,
        "count": 10,
        "format": "json",
        "autoload": "true",
        "cur_tab": 1,
        "from": "search_tab",
    }
    resp = session.get(api, params=params, timeout=30)
    if resp.status_code >= 400:
        return "", ""
    data = resp.json() if resp.headers.get("Content-Type", "").lower().startswith("application/json") else resp.json()
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return "", ""

    # Pick the first reasonable item.
    for it in items:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()
        abstract = str(it.get("abstract") or it.get("description") or it.get("content") or "").strip()
        if title and abstract:
            clean = re.sub(r"\s+", " ", abstract).strip()
            excerpt = build_excerpt(clean)
            return excerpt, clean

    return "", ""


def is_target_site(site: str) -> bool:
    return site in {"toutiao", "weixin", "douban", "zhihu", "netease"}


def strip_html_to_text(html: str) -> str:
    s = (html or "").strip()
    if not s:
        return ""
    s = re.sub(r"(?is)<(script|style|noscript)\b.*?>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</p\s*>", "\n", s)
    s = re.sub(r"(?i)</div\s*>", "\n", s)
    s = re.sub(r"(?is)<[^>]+>", " ", s)
    s = unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def extract_netease_docid(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        p = urlsplit(raw)
    except Exception:
        return ""
    # Common: https://c.m.163.com/news/a/<DOCID>.html
    m = re.search(r"/news/a/([0-9A-Za-z]+)\.html", p.path or "")
    if m:
        return m.group(1)
    # Fallback: any .../<DOCID>.html where DOCID looks like Netease docid.
    m = re.search(r"/([0-9A-Za-z]{10,})\.html", p.path or "")
    if m:
        return m.group(1)
    return ""


def fetch_netease_article(session: requests.Session, url: str, title_hint: str) -> Tuple[str, str, str, str]:
    docid = extract_netease_docid(url)
    if not docid:
        raise RuntimeError("netease_no_docid")

    api = f"https://c.m.163.com/nc/article/{docid}/full.html"
    resp = session.get(api, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"netease_api_http_{resp.status_code}")

    data = resp.json() if resp.headers.get("Content-Type", "").lower().startswith("application/json") else resp.json()
    if not isinstance(data, dict) or docid not in data or not isinstance(data.get(docid), dict):
        raise RuntimeError("netease_api_bad_json")

    doc = data.get(docid) or {}
    title = str(doc.get("title") or "").strip() or (title_hint or "").strip() or "网易新闻"
    canonical = str(doc.get("shareLink") or "").strip() or url
    body_html = str(doc.get("body") or "").strip()
    clean_text = strip_html_to_text(body_html)
    if not clean_text or len(clean_text) < 120:
        # try headText/shareDigest as last resort
        clean_text = str(doc.get("headText") or doc.get("shareDigest") or "").strip()
        clean_text = re.sub(r"\s+", " ", clean_text).strip()

    excerpt = build_excerpt(clean_text)
    return title, canonical, excerpt, clean_text


def tophub_extract_items(payload: object) -> List[dict]:
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


def fetch_tophub_node_urls(
    session: requests.Session, *, api_key: str, hashid: str, date_iso: str, limit: int = 50
) -> List[Tuple[str, str]]:
    hk = (hashid or "").strip()
    ak = (api_key or "").strip()
    if not hk or not ak:
        return []

    base = "https://api.tophubdata.com"
    headers = {"Authorization": ak}

    items: List[dict] = []
    try:
        r = session.get(f"{base}/nodes/{hk}/historys", params={"date": date_iso}, headers=headers, timeout=30)
        if r.status_code < 400:
            items = tophub_extract_items(r.json())
    except Exception:
        items = []

    if not items:
        try:
            r = session.get(f"{base}/nodes/{hk}", headers=headers, timeout=30)
            if r.status_code < 400:
                items = tophub_extract_items(r.json())
        except Exception:
            items = []

    out: List[Tuple[str, str]] = []
    for it in items[: max(1, int(limit or 50))]:
        u = str(it.get("url") or "").strip()
        t = str(it.get("title") or "").strip()
        if u:
            out.append((u, t))
    return out


def strip_html_basic(html: str) -> str:
    s = html or ""
    s = re.sub(r"(?is)<(script|style|noscript|svg|canvas|iframe)\b.*?>.*?</\1>", " ", s)
    s = re.sub(r"(?is)<!--.*?-->", " ", s)
    s = re.sub(r"(?is)<br\s*/?>", "\n", s)
    s = re.sub(r"(?is)</p\s*>", "\n", s)
    s = re.sub(r"(?is)<[^>]+>", " ", s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def pick_title(html: str, fallback: str) -> str:
    for pat in [
        r'(?is)<meta\s+property="og:title"\s+content="([^"]+)"',
        r"(?is)<meta\s+name=\"title\"\s+content=\"([^\"]+)\"",
        r"(?is)<title[^>]*>(.*?)</title>",
    ]:
        m = re.search(pat, html or "")
        if m:
            t = re.sub(r"\s+", " ", (m.group(1) or "").strip())
            if t:
                return t[:200]
    return (fallback or "").strip()[:200]

def pick_meta_description(html: str) -> str:
    for pat in [
        r'(?is)<meta\s+name="description"\s+content="([^"]+)"',
        r'(?is)<meta\s+property="og:description"\s+content="([^"]+)"',
    ]:
        m = re.search(pat, html or "")
        if m:
            t = re.sub(r"\s+", " ", (m.group(1) or "").strip())
            if t:
                return t
    return ""


def pick_canonical(html: str, base_url: str) -> str:
    m = re.search(r'(?is)<link\s+rel="canonical"\s+href="([^"]+)"', html or "")
    if not m:
        return ""
    href = (m.group(1) or "").strip()
    if not href:
        return ""
    return href


def build_excerpt(text: str, min_len: int = 200, max_len: int = 500) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    t = re.sub(r"\s+", " ", t)
    if len(t) <= max_len:
        return t
    cut = t[: max_len + 1]
    # try cut at sentence end
    for sep in ["。", "！", "？", ".", "!", "?"]:
        idx = cut.rfind(sep)
        if idx >= min_len:
            return cut[: idx + 1].strip()
    return t[:max_len].strip()


@dataclass
class Hotspot:
    source_id: str
    platform: str
    title: str
    url: str


def parse_hotspots_txt(txt: str) -> List[Hotspot]:
    out: List[Hotspot] = []
    for line in (txt or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\d+)\.\s+(.*)$", line)
        if not m:
            continue
        source_id = m.group(1)
        rest = m.group(2)
        um = re.search(r"\[URL:([^\]]+)\]", rest)
        if not um:
            continue
        url = (um.group(1) or "").strip()
        if not url:
            continue
        pm = re.search(r"\[platform=([^\]]+)\]", rest)
        platform = (pm.group(1) or "").strip() if pm else ""

        title = rest
        title = re.sub(r"\[platform=[^\]]+\]", " ", title)
        title = re.sub(r"\[platform_id=[^\]]+\]", " ", title)
        title = re.sub(r"\[URL:[^\]]+\]", " ", title)
        title = re.sub(r"\[MOBILE:[^\]]+\]", " ", title)
        title = re.sub(r"\[RANK:[^\]]+\]", " ", title)
        title = re.sub(r"\s+", " ", title).strip()

        out.append(Hotspot(source_id=source_id, platform=platform, title=title, url=url))
    return out


def build_hotspots_key(date_iso: str) -> str:
    y, m, d = date_iso.split("-", 2)
    return f"ai-hotspots/{y}/{m}/{d}/hotspots.txt"


def build_material_key(date_iso: str, normalized_url: str) -> str:
    y, m, d = date_iso.split("-", 2)
    h = sha1_hex(normalized_url)
    return f"ai-materials/{y}/{m}/{d}/web/{h}.json"


class R2Client:
    def __init__(self):
        if not HAS_BOTO3:
            raise RuntimeError("boto3 is required for this task. Install it: pip install boto3")

        endpoint = os.environ.get("S3_ENDPOINT_URL", "").strip()
        bucket = os.environ.get("S3_BUCKET_NAME", "").strip()
        ak = os.environ.get("S3_ACCESS_KEY_ID", "").strip()
        sk = os.environ.get("S3_SECRET_ACCESS_KEY", "").strip()
        region = os.environ.get("S3_REGION", "").strip() or "auto"

        if not endpoint or not bucket or not ak or not sk:
            raise RuntimeError("Missing R2 env vars: S3_ENDPOINT_URL/S3_BUCKET_NAME/S3_ACCESS_KEY_ID/S3_SECRET_ACCESS_KEY")

        self.bucket = bucket
        cfg = BotoConfig(s3={"addressing_style": "virtual"}, signature_version="s3v4")
        self.s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=ak,
            aws_secret_access_key=sk,
            region_name=region,
            config=cfg,
        )

    def get_text(self, key: str) -> str:
        resp = self.s3.get_object(Bucket=self.bucket, Key=key)
        body = resp["Body"].read()
        return body.decode("utf-8", errors="replace")

    def head(self, key: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:
            code = str(getattr(e, "response", {}).get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            return False
        except Exception:
            return False

    def get_json(self, key: str) -> Optional[Dict]:
        try:
            txt = self.get_text(key)
            return json.loads(txt)
        except Exception:
            return None

    def put_json(self, key: str, data: Dict) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType="application/json; charset=utf-8",
        )


def fetch_html(session: requests.Session, url: str, timeout: int = 20, retries: int = 2) -> Tuple[int, str, str]:
    last_err = ""
    for i in range(retries + 1):
        try:
            resp = session.get(url, timeout=timeout, allow_redirects=True)
            status = int(resp.status_code)
            ctype = resp.headers.get("Content-Type", "")
            text = resp.text or ""
            return status, ctype, text
        except Exception as e:
            last_err = str(e)
            if i >= retries:
                break
    raise RuntimeError(last_err or "fetch failed")


def extract_content(html: str, fallback_title: str) -> Tuple[str, str, str, str]:
    title = pick_title(html, fallback_title)
    canonical = pick_canonical(html, "")
    desc = pick_meta_description(html)

    # 1) Try trafilatura if installed
    clean_text = ""
    try:
        import trafilatura  # type: ignore

        clean_text = trafilatura.extract(html, include_comments=False, include_tables=False) or ""
    except Exception:
        clean_text = ""

    if not clean_text:
        clean_text = strip_html_basic(html)

    clean_text = re.sub(r"\s+\n", "\n", clean_text).strip()
    if clean_text and len(clean_text) > 6000:
        clean_text = clean_text[:6000].rstrip() + " ...(truncated)"

    excerpt = build_excerpt(desc) if desc else ""
    if not excerpt:
        excerpt = build_excerpt(clean_text)
    return title, canonical, excerpt, clean_text


def build_fetch_candidates(url: str) -> List[str]:
    raw = (url or "").strip()
    if not raw:
        return []
    out = [raw]
    try:
        p = urlsplit(raw)
        host = (p.netloc or "").lower()
        if host == "www.toutiao.com":
            out.append(urlunsplit((p.scheme or "https", "m.toutiao.com", p.path, p.query, "")))
    except Exception:
        pass
    # de-dup preserving order
    seen = set()
    uniq = []
    for u in out:
        if u in seen:
            continue
        seen.add(u)
        uniq.append(u)
    return uniq


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--log", default="")
    args = ap.parse_args(argv)

    date_iso = args.date.strip()
    assert_iso_date(date_iso)
    concurrency = max(1, min(5, int(args.concurrency or 2)))

    force = os.environ.get("FORCE", "").strip() == "1"

    r2 = R2Client()

    hotspots_key = build_hotspots_key(date_iso)
    hotspots_txt = r2.get_text(hotspots_key)
    hotspots = parse_hotspots_txt(hotspots_txt)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )

    urls: List[Tuple[str, str]] = []
    for hs in hotspots:
        site = detect_site(hs.url)
        if not is_target_site(site):
            continue
        urls.append((hs.url, hs.title))

    # Extra input: TopHub "网易新闻实时热榜" (hashid=ENeYa4DeY4) as a web-enhancement allowlist source.
    tophub_api_key = os.environ.get("TOPHUB_API_KEY", "").strip()
    if tophub_api_key:
        netease_hashid = os.environ.get("TOPHUB_NETEASE_HASHID", "ENeYa4DeY4").strip() or "ENeYa4DeY4"
        try:
            extra = fetch_tophub_node_urls(
                session, api_key=tophub_api_key, hashid=netease_hashid, date_iso=date_iso, limit=50
            )
            for u, t in extra:
                if is_target_site(detect_site(u)):
                    urls.append((u, t))
        except Exception:
            pass

    # De-duplicate by normalized URL (same URL across sources).
    seen: set[str] = set()
    deduped: List[Tuple[str, str]] = []
    for u, t in urls:
        nu = normalize_url_for_hash(u)
        if not nu or nu in seen:
            continue
        seen.add(nu)
        deduped.append((u, t))
    urls = deduped

    total = len(urls)
    ok = 0
    skipped = 0
    fail = 0
    reasons: Counter[str] = Counter()

    # simple sequential with small concurrency via threads (requests is blocking)
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def one(url: str, title_hint: str) -> Tuple[str, str]:
        normalized = normalize_url_for_hash(url)
        if not normalized:
            return "skip", "bad_url"

        site = detect_site(normalized)
        key = build_material_key(date_iso, normalized)

        if not force and r2.head(key):
            existing = r2.get_json(key) or {}
            blocked = str((existing.get("meta") or {}).get("blocked_reason") or "").strip()
            excerpt = str(existing.get("excerpt") or "").strip()
            if excerpt and not blocked:
                return "skipped", "exists"

        try:
            best: Tuple[str, str, str, str] | None = None
            chosen_url = url
            last_err = ""
            skip_html_candidates = False

            # Toutiao: the URL we have is often a search/trending page, not an article page.
            # Try keyword-based API snippet first to get something usable for Step3/Step4.
            if site == "toutiao":
                kw = extract_toutiao_keyword(url, title_hint)
                excerpt_api, clean_api = fetch_toutiao_search_snippet(session, kw)
                if excerpt_api and clean_api:
                    best = (kw or title_hint or "今日头条", "", excerpt_api, clean_api)
                    chosen_url = url

            # Netease mobile pages: use the public JSON endpoint for stable title/body extraction.
            if site == "netease":
                title_n, canonical_n, excerpt_n, clean_n = fetch_netease_article(session, url, title_hint)
                if excerpt_n and clean_n:
                    best = (title_n, canonical_n, excerpt_n, clean_n)
                    chosen_url = url
                    skip_html_candidates = True

            if not skip_html_candidates:
                for cand in build_fetch_candidates(url):
                    try:
                        status, ctype, html = fetch_html(session, cand)
                        if status >= 400:
                            raise RuntimeError(f"http_{status}")
                        if "text/html" not in ctype.lower() and "application/xhtml" not in ctype.lower():
                            # still try
                            pass

                        title, canonical, excerpt, clean_text = extract_content(html, title_hint)

                        blocked_reason = ""
                        if not excerpt or len(excerpt) < 80:
                            blocked_reason = "empty_extracted"
                        if re.search(r"(登录|登陆|继续访问|请先登录|扫码|验证|安全验证)", clean_text[:1200]):
                            blocked_reason = blocked_reason or "login_wall"

                        best = (title, canonical, excerpt, clean_text)
                        chosen_url = cand
                        if not blocked_reason:
                            break
                    except Exception as e:
                        last_err = str(e)
                        continue

            if not best:
                raise RuntimeError(last_err or "fetch_failed")

            title, canonical, excerpt, clean_text = best
            blocked_reason = ""
            if not excerpt or len(excerpt) < 80:
                blocked_reason = "empty_extracted"
            if re.search(r"(登录|登陆|继续访问|请先登录|扫码|验证|安全验证)", clean_text[:1200]):
                blocked_reason = blocked_reason or "login_wall"

            doc = {
                "schema_version": 1,
                "title": title,
                "url": url,
                "site": detect_site(url),
                "canonical_url": canonical,
                "fetched_at": now_iso(),
                "excerpt": excerpt,
                "clean_text": clean_text,
                "meta": {
                    "normalized_url": normalized,
                    "hash": sha1_hex(normalized),
                    "blocked_reason": blocked_reason,
                    "fetched_url": chosen_url,
                },
            }
            r2.put_json(key, doc)
            return ("ok" if not blocked_reason else "blocked"), (blocked_reason or "ok")
        except Exception as e:
            doc = {
                "schema_version": 1,
                "title": "",
                "url": url,
                "site": detect_site(url),
                "canonical_url": "",
                "fetched_at": now_iso(),
                "excerpt": "",
                "clean_text": "",
                "meta": {
                    "normalized_url": normalize_url_for_hash(url),
                    "hash": sha1_hex(normalize_url_for_hash(url)),
                    "blocked_reason": "fetch_failed",
                    "error": str(e)[:400],
                },
            }
            try:
                r2.put_json(build_material_key(date_iso, normalize_url_for_hash(url) or url), doc)
            except Exception:
                pass
            return "fail", "fetch_failed"

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = [ex.submit(one, u, t) for u, t in urls]
        for f in as_completed(futs):
            status, reason = f.result()
            if status == "ok":
                ok += 1
            elif status == "skipped":
                skipped += 1
            else:
                fail += 1
            reasons[reason] += 1

    summary = {
        "date": date_iso,
        "hotspots_key": hotspots_key,
        "total_urls": total,
        "ok": ok,
        "skipped": skipped,
        "failed_or_blocked": fail,
        "top_reasons": reasons.most_common(10),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
