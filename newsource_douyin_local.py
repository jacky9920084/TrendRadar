# coding=utf-8
"""
Douyin local extraction task (standalone script):
- Fetch daily douyin hotlist from Hotspot-Spark
- Resolve mp4_url via local video_spider
- Download mp4 to local temp file (deleted after run)
- Upload mp4 to Gemini Files API, generate strict JSON {text,visual,why_hot}
- Upload per-video materials to R2: ai-materials/YYYY/MM/DD/douyin/{videoId}.json

Design goals:
- Cloudflare Hotspot-Spark does NOT download mp4 / call Gemini for extraction.
- Every item writes a material JSON (success or placeholder) to keep stats continuous.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

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


def now_iso() -> str:
    # Python 3.13: avoid deprecated utcnow(); always generate explicit UTC timestamp.
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def assert_iso_date(date_str: str) -> None:
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str or ""):
        raise ValueError(f"Invalid date format: {date_str} (expected YYYY-MM-DD).")


def iso_parts(date_iso: str) -> Tuple[str, str, str]:
    y, m, d = (date_iso or "").split("-")
    return y, m, d


def sanitize_text(value: Any) -> str:
    s = value if isinstance(value, str) else ""
    s = re.sub(r"[\u0000-\u001f]", " ", s)
    s = s.replace('"', "'").strip()
    return s


def truncate_with_marker(s: str, max_chars: int) -> str:
    text = (s or "").strip()
    if len(text) <= max_chars:
        return text
    suffix = " ...(truncated)"
    head_len = max(0, max_chars - len(suffix))
    return text[:head_len].rstrip() + suffix


def extract_douyin_video_id(url: str) -> Optional[str]:
    s = (url or "").strip()
    m = re.search(r"douyin\.com/video/(\d+)", s, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def build_material_key(prefix: str, date_iso: str, video_id: str) -> str:
    y, m, d = iso_parts(date_iso)
    p = (prefix or "ai-materials").strip().strip("/")
    return f"{p}/{y}/{m}/{d}/douyin/{video_id}.json"


def build_global_cache_key(prefix: str, video_id: str) -> str:
    p = (prefix or "ai-materials").strip().strip("/")
    return f"{p}/douyin-by-id/{video_id}.json"


def is_usable_material_doc(doc: Dict[str, Any]) -> bool:
    meta = doc.get("meta") if isinstance(doc, dict) else None
    blocked = str((meta or {}).get("blocked_reason") or "").strip()
    if blocked:
        return False
    text = str(doc.get("text") or "").strip()
    visual = str(doc.get("visual") or "").strip()
    why_hot = str(doc.get("why_hot") or "").strip()
    return bool(text or visual or why_hot)


class R2Client:
    def __init__(self) -> None:
        if not HAS_BOTO3:
            raise RuntimeError("boto3 is required for R2 operations. Install dependencies first.")

        endpoint = (os.getenv("S3_ENDPOINT_URL") or "").strip()
        bucket = (os.getenv("S3_BUCKET_NAME") or "").strip()
        ak = (os.getenv("S3_ACCESS_KEY_ID") or "").strip()
        sk = (os.getenv("S3_SECRET_ACCESS_KEY") or "").strip()
        region = (os.getenv("S3_REGION") or "auto").strip()

        if not endpoint or not bucket or not ak or not sk:
            raise RuntimeError("Missing S3/R2 env vars: S3_ENDPOINT_URL/S3_BUCKET_NAME/S3_ACCESS_KEY_ID/S3_SECRET_ACCESS_KEY")

        self.bucket = bucket
        cfg = None
        try:
            cfg = BotoConfig(signature_version="s3v4")
        except Exception:
            cfg = None

        self.s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=ak,
            aws_secret_access_key=sk,
            region_name=region,
            config=cfg,
        )

    def head(self, key: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:  # type: ignore[misc]
            code = str(getattr(e, "response", {}).get("Error", {}).get("Code", "") or "")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            return False
        except Exception:
            return False

    def get_json(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=key)
            body = obj["Body"].read()
            return json.loads(body.decode("utf-8"))
        except Exception:
            return None

    def put_json(self, key: str, doc: Dict[str, Any]) -> None:
        body = json.dumps(doc, ensure_ascii=False).encode("utf-8")
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType="application/json; charset=utf-8")


def http_get_json(url: str, timeout: int = 30) -> Any:
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def http_post_form_json(url: str, form: Dict[str, str], timeout: int = 60) -> Any:
    resp = requests.post(url, data=form, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def fetch_hotlist(base: str, date_iso: str) -> List[Dict[str, Any]]:
    url = f"{base.rstrip('/')}/api/hotlist/douyin?date={date_iso}&refresh=1"
    data = http_get_json(url, timeout=30)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [it for it in items if isinstance(it, dict)]


def analyze_with_video_spider(spider_base: str, share_link: str) -> Dict[str, Any]:
    url = f"{spider_base.rstrip('/')}/analysis"
    return http_post_form_json(url, {"share_link": share_link}, timeout=90)


def download_mp4(url: str, dst_path: str, timeout: int, max_bytes: int) -> Tuple[int, str]:
    with requests.get(url, stream=True, timeout=timeout, allow_redirects=True) as r:
        r.raise_for_status()
        ctype = str(r.headers.get("Content-Type") or "")
        clen = r.headers.get("Content-Length")
        if clen:
            try:
                n = int(clen)
                if n > max_bytes:
                    raise RuntimeError(f"mp4_too_large: content-length={n} max={max_bytes}")
            except ValueError:
                pass

        size = 0
        with open(dst_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise RuntimeError(f"mp4_too_large: bytes>{max_bytes}")
                f.write(chunk)
        return size, ctype


def gemini_upload_file_raw(api_key: str, mime_type: str, file_path: str) -> Dict[str, Any]:
    url = f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={api_key}"
    with open(file_path, "rb") as f:
        resp = requests.post(
            url,
            headers={"X-Goog-Upload-Protocol": "raw", "Content-Type": mime_type},
            data=f,
            timeout=180,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"gemini_upload_failed: http_{resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    file = data.get("file") if isinstance(data, dict) else None
    if not isinstance(file, dict) or not file.get("name") or not file.get("uri"):
        raise RuntimeError("gemini_upload_failed: empty file uri")
    return file


def gemini_wait_file_active(api_key: str, file_name: str, timeout_sec: int = 60) -> None:
    start = time.time()
    while True:
        url = f"https://generativelanguage.googleapis.com/v1beta/{file_name}?key={api_key}"
        resp = requests.get(url, timeout=30)
        if resp.ok:
            data = resp.json()
            state = ""
            if isinstance(data, dict):
                if isinstance(data.get("file"), dict) and isinstance(data["file"].get("state"), str):
                    state = data["file"]["state"]
                elif isinstance(data.get("state"), str):
                    state = data["state"]
            if state in {"ACTIVE", "STATE_ACTIVE"}:
                return
        if time.time() - start > timeout_sec:
            raise RuntimeError(f"gemini_file_not_active: {file_name}")
        time.sleep(0.8)


def gemini_generate_with_file(
    api_key: str,
    model: str,
    prompt_text: str,
    file_uri: str,
    mime_type: str,
    temperature: float,
    max_output_tokens: int,
) -> Tuple[str, Optional[Any]]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt_text},
                    {"fileData": {"fileUri": file_uri, "mimeType": mime_type}},
                ],
            }
        ],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_output_tokens},
    }
    resp = requests.post(url, headers={"Content-Type": "application/json", "x-goog-api-key": api_key}, json=body, timeout=180)
    if resp.status_code >= 400:
        raise RuntimeError(f"gemini_generate_failed: http_{resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    parts = []
    try:
        cand0 = (data.get("candidates") or [None])[0]
        content = (cand0 or {}).get("content") or {}
        for p in content.get("parts") or []:
            t = p.get("text")
            if isinstance(t, str):
                parts.append(t)
    except Exception:
        parts = []
    text = "".join(parts).strip()
    if not text:
        raise RuntimeError("gemini_generate_failed: empty_text")
    return text, data.get("usageMetadata") if isinstance(data, dict) else None


def gemini_delete_file(api_key: str, file_name: str) -> None:
    if not file_name:
        return
    url = f"https://generativelanguage.googleapis.com/v1beta/{file_name}?key={api_key}"
    resp = requests.delete(url, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"gemini_delete_failed: http_{resp.status_code}: {resp.text[:300]}")


def parse_json_object_strict(text: str) -> Dict[str, Any]:
    s = (text or "").strip()
    if not s:
        raise ValueError("empty_output")
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        slice_text = s[start : end + 1]
        obj = json.loads(slice_text)
        if isinstance(obj, dict):
            return obj
    raise ValueError("not_json_object")


def load_prompt(prompt_path: str) -> str:
    raw = open(prompt_path, "r", encoding="utf-8").read()
    hard = (
        '只输出 JSON（禁止任何解释、markdown、代码块）。\\n'
        '输出必须是单个 JSON 对象，字段固定为：{"text":"...","visual":"...","why_hot":"..."}。\\n'
        '三个字段必须都出现；拿不到就填空字符串。\\n\\n'
    )
    return hard + raw


def write_log(log_path: str, msg: str) -> None:
    if not log_path:
        return
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    with open(log_path, "a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")


def retry(action, retries: int, base_sleep: float) -> Any:
    last = None
    for i in range(retries + 1):
        try:
            return action()
        except Exception as e:
            last = e
            if i >= retries:
                break
            time.sleep(min(10.0, base_sleep * (2**i)))
    raise last  # type: ignore[misc]


def process_one(
    r2: R2Client,
    date_iso: str,
    materials_prefix: str,
    hotspot_base: str,
    spider_base: str,
    gemini_api_key: str,
    gemini_model: str,
    prompt_text: str,
    max_mp4_bytes: int,
    force: bool,
    item: Dict[str, Any],
    log_path: str,
) -> Tuple[str, str, str, int]:
    url = str(item.get("url") or "").strip()
    item_id = str(item.get("item_id") or "").strip()
    title_hint = str(item.get("title") or "").strip()
    video_id = extract_douyin_video_id(url)
    storage_id = video_id or item_id
    if not storage_id:
        return "blocked", "missing_id", "miss", 0
    if not video_id:
        # Can't follow the global cache rule without a stable videoId; still write a placeholder for stats/debug.
        daily_key = build_material_key(materials_prefix, date_iso, storage_id)
        doc = {
            "schema_version": 1,
            "platform": "douyin",
            "item_id": storage_id,
            "source_url": url,
            "title": sanitize_text(title_hint),
            "kind": "video",
            "text": "",
            "visual": "",
            "why_hot": "",
            "meta": {
                "fetched_at": now_iso(),
                "normalized_source_id": storage_id,
                "mp4_url": "",
                "thumbnail": sanitize_text(item.get("thumbnail")),
                "blocked_reason": "missing_video_id",
                "error": "Cannot extract videoId from video_url.",
                "attempts": 0,
                "gemini": {"model": gemini_model},
            },
        }
        try:
            r2.put_json(daily_key, doc)
        except Exception:
            pass
        write_log(log_path, f"{storage_id} reuse_miss")
        return "blocked", "missing_video_id", "miss", 0

    # 1) Extract videoId (done above)
    # 2) Global cache check (cross-day / cross-machine shared)
    global_key = build_global_cache_key(materials_prefix, video_id)
    daily_key = build_material_key(materials_prefix, date_iso, video_id)

    if not force and r2.head(global_key):
        cached = r2.get_json(global_key) or {}
        if isinstance(cached, dict) and is_usable_material_doc(cached):
            meta = cached.get("meta")
            if not isinstance(meta, dict):
                meta = {}
                cached["meta"] = meta
            meta["reused_from"] = global_key
            meta["reused_at"] = now_iso()
            meta["reuse_hit"] = True
            try:
                r2.put_json(daily_key, cached)
            except Exception:
                # If we can't write daily key, treat as blocked for visibility.
                write_log(log_path, f"{video_id} reuse_hit daily_write_failed")
                return "blocked", "daily_write_failed", "hit", 0
            write_log(log_path, f"{video_id} reuse_hit")
            return "ok", "ok", "hit", 0

    write_log(log_path, f"{video_id} reuse_miss")

    doc: Dict[str, Any] = {
        "schema_version": 1,
        "platform": "douyin",
        "item_id": video_id,
        "source_url": url,
        "title": sanitize_text(title_hint),
        "kind": "video",
        "text": "",
        "visual": "",
        "why_hot": "",
        "meta": {
            "fetched_at": now_iso(),
            "normalized_source_id": video_id,
            "mp4_url": "",
            "thumbnail": sanitize_text(item.get("thumbnail")),
            "blocked_reason": "pending",
            "error": "",
            "attempts": 0,
            "gemini": {"model": gemini_model},
        },
    }

    tmp_path = ""
    gem_file_name = ""
    gem_file_uri = ""
    attempts = 0
    gemini_calls = 0

    try:
        def do_spider():
            return analyze_with_video_spider(spider_base, url)

        spider = retry(do_spider, retries=2, base_sleep=0.8)
        data = spider.get("data") if isinstance(spider, dict) else None
        if not isinstance(data, dict):
            raise RuntimeError("video_spider_bad_response")

        resource = data.get("resource_path")
        if isinstance(resource, list):
            doc["meta"]["blocked_reason"] = "not_video_gallery"
            doc["meta"]["error"] = "video_spider returned resource_path as array (gallery)"
            r2.put_json(daily_key, doc)
            return "blocked", "not_video_gallery", "miss", gemini_calls

        mp4_url = str(resource or "").strip()
        if not mp4_url:
            raise RuntimeError("missing_resource_path")

        doc["meta"]["mp4_url"] = mp4_url
        doc["title"] = sanitize_text(str(data.get("title") or title_hint))
        cover = str(data.get("cover") or "").strip()
        if cover:
            doc["meta"]["thumbnail"] = sanitize_text(cover)

        attempts += 1
        doc["meta"]["attempts"] = attempts

        tmp_dir = tempfile.mkdtemp(prefix=f"tr_douyin_{video_id}_")
        tmp_path = os.path.join(tmp_dir, f"{video_id}.mp4")
        size, ctype = retry(lambda: download_mp4(mp4_url, tmp_path, timeout=180, max_bytes=max_mp4_bytes), retries=2, base_sleep=0.8)
        mime = "video/mp4"
        if "video/" in (ctype or ""):
            mime = ctype.split(";")[0].strip()

        file = retry(lambda: gemini_upload_file_raw(gemini_api_key, mime, tmp_path), retries=1, base_sleep=0.8)
        gem_file_name = str(file.get("name") or "")
        gem_file_uri = str(file.get("uri") or "")
        doc["meta"]["gemini"]["file_name"] = gem_file_name
        doc["meta"]["gemini"]["file_uri"] = gem_file_uri

        gemini_wait_file_active(gemini_api_key, gem_file_name, timeout_sec=90)

        gemini_calls += 1
        out_text, usage = retry(
            lambda: gemini_generate_with_file(
                api_key=gemini_api_key,
                model=gemini_model,
                prompt_text=prompt_text,
                file_uri=gem_file_uri,
                mime_type=mime,
                temperature=0.8,
                max_output_tokens=30000,
            ),
            retries=1,
            base_sleep=0.8,
        )

        if usage is not None:
            doc["meta"]["gemini"]["usage"] = usage

        parsed = parse_json_object_strict(out_text)
        text = truncate_with_marker(sanitize_text(parsed.get("text")), 4000)
        visual = truncate_with_marker(sanitize_text(parsed.get("visual")), 1500)
        why_hot = truncate_with_marker(sanitize_text(parsed.get("why_hot")), 300)
        doc["text"] = text
        doc["visual"] = visual
        doc["why_hot"] = why_hot
        doc["meta"]["blocked_reason"] = ""
        doc["meta"]["error"] = ""
        doc["meta"]["fetched_at"] = now_iso()

        # 3) On success: write global cache, then daily material key.
        try:
            global_doc = json.loads(json.dumps(doc, ensure_ascii=False))
            gmeta = global_doc.get("meta")
            if isinstance(gmeta, dict):
                gmeta.pop("reused_from", None)
                gmeta.pop("reused_at", None)
                gmeta.pop("reuse_hit", None)
            r2.put_json(global_key, global_doc)
        except Exception as e:
            write_log(log_path, f"{video_id} global_cache_write_failed {str(e)[:160]}")

        r2.put_json(daily_key, doc)
        return "ok", "ok", "miss", gemini_calls
    except Exception as e:
        br = str(doc["meta"].get("blocked_reason") or "").strip()
        if not br or br == "pending":
            br = "extract_failed"
        doc["meta"]["blocked_reason"] = br
        doc["meta"]["error"] = str(e)[:400]
        doc["meta"]["attempts"] = attempts
        try:
            r2.put_json(daily_key, doc)
        except Exception:
            pass
        return "blocked", str(doc["meta"]["blocked_reason"] or "extract_failed"), "miss", gemini_calls
    finally:
        if gem_file_name:
            try:
                gemini_delete_file(gemini_api_key, gem_file_name)
            except Exception:
                pass
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            try:
                os.rmdir(os.path.dirname(tmp_path))
            except Exception:
                pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--log", default="")
    args = ap.parse_args()

    date_iso = (args.date or "").strip()
    assert_iso_date(date_iso)

    concurrency = int(args.concurrency or 1)
    if concurrency < 1:
        concurrency = 1
    if concurrency > 3:
        concurrency = 3

    hotspot_base = (os.getenv("HOTSPARK_BASE") or "https://hot-sparks.jacky.onl").strip().rstrip("/")
    spider_base = (os.getenv("VIDEO_SPIDER_BASE") or "http://127.0.0.1:8080").strip().rstrip("/")
    materials_prefix = (os.getenv("AI_MATERIALS_PREFIX") or "ai-materials").strip().strip("/")
    gemini_api_key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
    gemini_model = (os.getenv("GEMINI_MODEL") or "gemini-3-flash-preview").strip()
    prompt_path = (os.getenv("GEMINI_PROMPT_PATH") or "").strip()
    force = str(os.getenv("FORCE") or "").strip() == "1"
    max_mp4_bytes = int(os.getenv("MAX_MP4_BYTES") or str(200 * 1024 * 1024))

    if not gemini_api_key:
        raise RuntimeError("Missing GEMINI_API_KEY (or GOOGLE_API_KEY).")
    if not prompt_path or not os.path.exists(prompt_path):
        raise RuntimeError(f"Missing GEMINI_PROMPT_PATH: {prompt_path}")

    prompt_text = load_prompt(prompt_path)
    r2 = R2Client()

    items = fetch_hotlist(hotspot_base, date_iso)
    if not items:
        print(json.dumps({"date": date_iso, "total": 0, "ok": 0, "skipped": 0, "blocked": 0, "reason_top": []}, ensure_ascii=False))
        return 0

    write_log(args.log, f"Hotlist loaded: {len(items)} items")

    from concurrent.futures import ThreadPoolExecutor, as_completed

    counters: Counter[str] = Counter()
    ok = 0
    blocked = 0
    reuse_hits = 0
    reuse_misses = 0
    gemini_calls = 0

    def one(it: Dict[str, Any]) -> Tuple[str, str, str, int]:
        return process_one(
            r2=r2,
            date_iso=date_iso,
            materials_prefix=materials_prefix,
            hotspot_base=hotspot_base,
            spider_base=spider_base,
            gemini_api_key=gemini_api_key,
            gemini_model=gemini_model,
            prompt_text=prompt_text,
            max_mp4_bytes=max_mp4_bytes,
            force=force,
            item=it,
            log_path=args.log,
        )

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futs = [ex.submit(one, it) for it in items]
        for f in as_completed(futs):
            status, reason, reuse_state, gem_calls = f.result()
            gemini_calls += int(gem_calls or 0)
            if reuse_state == "hit":
                reuse_hits += 1
            else:
                reuse_misses += 1
            if status == "ok":
                ok += 1
            else:
                blocked += 1
                counters[reason] += 1

    summary = {
        "date": date_iso,
        "total_items": len(items),
        "ok": ok,
        "blocked": blocked,
        "reuse_hits": reuse_hits,
        "reuse_misses": reuse_misses,
        "gemini_calls": gemini_calls,
        "blocked_reason_top": counters.most_common(10),
    }
    write_log(args.log, f"Summary reuse_hits={reuse_hits} reuse_misses={reuse_misses} gemini_calls={gemini_calls}")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
