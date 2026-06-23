import base64
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
EBAY_CLIENT_ID = os.environ["EBAY_CLIENT_ID"]
EBAY_CLIENT_SECRET = os.environ["EBAY_CLIENT_SECRET"]
EBAY_MARKETPLACE_ID = os.environ.get("EBAY_MARKETPLACE_ID", "EBAY_US")

WORKER_ID = os.environ.get("WORKER_ID", "github-actions-detail-worker")
JOB_BATCH_SIZE = int(os.environ.get("JOB_BATCH_SIZE", "25"))
MAX_API_CALLS = int(os.environ.get("MAX_API_CALLS", "50"))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "30"))
LOCK_STALE_MINUTES = int(os.environ.get("LOCK_STALE_MINUTES", "30"))
WORKER_RATE_LIMIT_BACKOFF_SECONDS = int(os.environ.get("WORKER_RATE_LIMIT_BACKOFF_SECONDS", "300"))

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

HTTP_SESSION = requests.Session()
EBAY_ACCESS_TOKEN_CACHE: Optional[str] = None


class WorkerRateLimitError(Exception):
    def __init__(self, sleep_seconds: int, message: str = "worker_rate_limited") -> None:
        super().__init__(message)
        self.sleep_seconds = sleep_seconds
        self.message = message


def log(message: str) -> None:
    print(message, flush=True)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def payload_hash(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def get_ebay_access_token() -> str:
    global EBAY_ACCESS_TOKEN_CACHE
    if EBAY_ACCESS_TOKEN_CACHE:
        return EBAY_ACCESS_TOKEN_CACHE

    token_url = "https://api.ebay.com/identity/v1/oauth2/token"
    basic = base64.b64encode(f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()).decode()
    headers = {
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }

    log("Requesting eBay application access token...")
    response = HTTP_SESSION.post(token_url, headers=headers, data=data, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    payload = response.json()
    EBAY_ACCESS_TOKEN_CACHE = payload["access_token"]
    log("eBay application token acquired.")
    return EBAY_ACCESS_TOKEN_CACHE


def release_stale_running_jobs() -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=LOCK_STALE_MINUTES)).isoformat()
    result = (
        supabase.table("enrichment_jobs")
        .select("id,status,job_type,locked_at")
        .eq("status", "running")
        .eq("job_type", "detail_fetch")
        .lt("locked_at", cutoff)
        .execute()
    )

    stale_jobs = result.data or []
    released = 0

    for job in stale_jobs:
        update = (
            supabase.table("enrichment_jobs")
            .update(
                {
                    "status": "queued",
                    "locked_at": None,
                    "worker_id": None,
                    "last_error": f"lock_released_after_{LOCK_STALE_MINUTES}m",
                    "updated_at": utc_now_iso(),
                    "available_at": utc_now_iso(),
                }
            )
            .eq("id", job["id"])
            .eq("status", "running")
            .execute()
        )
        if update.data:
            released += 1

    if released:
        log(f"Released {released} stale running detail jobs")
    return released


def claim_jobs(limit: int) -> List[Dict[str, Any]]:
    now_iso = utc_now_iso()

    result = (
        supabase.table("enrichment_jobs")
        .select("*")
        .eq("status", "queued")
        .eq("job_type", "detail_fetch")
        .lte("available_at", now_iso)
        .order("priority", desc=False)
        .order("created_at", desc=False)
        .limit(limit)
        .execute()
    )

    jobs = result.data or []
    claimed: List[Dict[str, Any]] = []

    for job in jobs:
        claim_result = (
            supabase.table("enrichment_jobs")
            .update(
                {
                    "status": "running",
                    "locked_at": now_iso,
                    "worker_id": WORKER_ID,
                    "attempt_count": (job.get("attempt_count") or 0) + 1,
                    "updated_at": now_iso,
                }
            )
            .eq("id", job["id"])
            .eq("status", "queued")
            .execute()
        )

        if claim_result.data:
            claimed.append(claim_result.data[0])

    return claimed


def fetch_item_detail(access_token: str, source_listing_id: str) -> Dict[str, Any]:
    url = f"https://api.ebay.com/buy/browse/v1/item/{source_listing_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": EBAY_MARKETPLACE_ID,
    }

    max_attempts = 3
    delay = 2
    last_retry_after: Optional[int] = None

    for attempt in range(1, max_attempts + 1):
        response = HTTP_SESSION.get(url, headers=headers, timeout=REQUEST_TIMEOUT)

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            sleep_for = int(retry_after) if retry_after and retry_after.isdigit() else delay
            last_retry_after = sleep_for
            log(f"Rate limited for {source_listing_id}; attempt {attempt}/{max_attempts}; retrying in {sleep_for}s")
            if attempt == max_attempts:
                raise WorkerRateLimitError(
                    sleep_seconds=max(sleep_for, WORKER_RATE_LIMIT_BACKOFF_SECONDS),
                    message="http_429",
                )
            time.sleep(sleep_for)
            delay = min(delay * 2, 60)
            continue

        if response.status_code >= 500:
            body = response.text[:1000] if response.text else ""
            if attempt == max_attempts:
                raise RuntimeError(f"http_{response.status_code}: {body}")
            log(f"Transient server error for {source_listing_id}: {response.status_code}; retrying in {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue

        if response.status_code >= 400:
            body = response.text[:1000] if response.text else ""
            raise RuntimeError(f"http_{response.status_code}: {body}")

        return response.json()

    raise WorkerRateLimitError(
        sleep_seconds=max(last_retry_after or 0, WORKER_RATE_LIMIT_BACKOFF_SECONDS),
        message="detail_fetch_failed_after_retries",
    )


def build_market_listing_patch(detail: Dict[str, Any]) -> Dict[str, Any]:
    shipping_value = None
    shipping_opts = detail.get("shippingOptions") or []
    if shipping_opts:
        cost = shipping_opts[0].get("shippingCost") or {}
        shipping_value = cost.get("value")

    image = detail.get("image") or {}
    seller = detail.get("seller") or {}
    price = detail.get("price") or {}
    item_location = detail.get("itemLocation") or {}
    condition = detail.get("condition")

    listing_status = "active"
    estimated_availability = detail.get("estimatedAvailabilities") or []
    if estimated_availability:
        est = estimated_availability[0]
        qty = est.get("estimatedAvailableQuantity")
        if qty == 0:
            listing_status = "ended"

    patch = {
        "raw_title": detail.get("title"),
        "listing_url": detail.get("itemWebUrl"),
        "raw_payload": detail,
        "seller_name": seller.get("username"),
        "seller_id": seller.get("username"),
        "current_price_value": float(price["value"]) if price.get("value") is not None else None,
        "current_price_currency": price.get("currency"),
        "shipping_value": float(shipping_value) if shipping_value is not None else None,
        "item_location": item_location.get("country") or item_location.get("city"),
        "primary_image_url": image.get("imageUrl"),
        "condition_text": condition,
        "listing_status": listing_status,
        "last_detail_refresh_at": utc_now_iso(),
        "last_seen_at": utc_now_iso(),
    }

    return {k: v for k, v in patch.items() if v is not None}


def upsert_raw_market_event(
    source: str,
    source_listing_id: str,
    payload: Dict[str, Any],
) -> None:
    row = {
        "source": source,
        "source_listing_id": source_listing_id,
        "event_type": "detail",
        "observed_at": utc_now_iso(),
        "payload_hash": payload_hash(payload),
        "payload_json": payload,
        "created_at": utc_now_iso(),
    }
    supabase.table("raw_market_events").insert(row).execute()


def update_market_listing(source: str, source_listing_id: str, patch: Dict[str, Any]) -> None:
    existing = (
        supabase.table("market_listings")
        .select("id")
        .eq("source", source)
        .eq("source_listing_id", source_listing_id)
        .limit(1)
        .execute()
    )

    if existing.data:
        (
            supabase.table("market_listings")
            .update(patch)
            .eq("source", source)
            .eq("source_listing_id", source_listing_id)
            .execute()
        )
    else:
        row = {
            "source": source,
            "source_listing_id": source_listing_id,
            **patch,
            "created_at": utc_now_iso(),
        }
        supabase.table("market_listings").insert(row).execute()


def mark_job_succeeded(job_id: str) -> None:
    (
        supabase.table("enrichment_jobs")
        .update(
            {
                "status": "done",
                "locked_at": None,
                "worker_id": WORKER_ID,
                "last_error": None,
                "updated_at": utc_now_iso(),
            }
        )
        .eq("id", job_id)
        .execute()
    )


def mark_job_failed(job: Dict[str, Any], error_text: str, delay_seconds: Optional[int] = None) -> None:
    attempt_count = job.get("attempt_count") or 1
    max_attempts = job.get("max_attempts") or 5
    terminal = attempt_count >= max_attempts

    next_status = "failed" if terminal else "queued"
    update_payload = {
        "status": next_status,
        "locked_at": None,
        "worker_id": WORKER_ID,
        "last_error": error_text[:1000],
        "updated_at": utc_now_iso(),
    }

    if not terminal:
        if delay_seconds is not None:
            available_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        else:
            backoff_minutes = min(60, 2 ** max(1, attempt_count))
            available_at = datetime.now(timezone.utc) + timedelta(minutes=backoff_minutes)
        update_payload["available_at"] = available_at.isoformat()

    (
        supabase.table("enrichment_jobs")
        .update(update_payload)
        .eq("id", job["id"])
        .execute()
    )


def requeue_unprocessed_jobs(jobs: List[Dict[str, Any]], start_index: int, reason: str, delay_seconds: int) -> int:
    requeued = 0
    for job in jobs[start_index:]:
        mark_job_failed(job, reason, delay_seconds=delay_seconds)
        requeued += 1
    return requeued


def process_job(access_token: str, job: Dict[str, Any]) -> Tuple[bool, str]:
    source = job["source"]
    source_listing_id = job["source_listing_id"]

    if source != "ebay":
        return False, f"unsupported_source:{source}"

    detail = fetch_item_detail(access_token, source_listing_id)
    upsert_raw_market_event(source, source_listing_id, detail)
    patch = build_market_listing_patch(detail)
    update_market_listing(source, source_listing_id, patch)
    mark_job_succeeded(job["id"])
    return True, source_listing_id


def main() -> None:
    log("Starting detail fetch worker...")
    released = release_stale_running_jobs()
    access_token = get_ebay_access_token()

    log("Claiming jobs...")
    jobs = claim_jobs(JOB_BATCH_SIZE)
    log(f"Claimed {len(jobs)} detail jobs")

    if not jobs:
        log(
            json.dumps(
                {
                    "released_stale_jobs": released,
                    "claimed": 0,
                    "processed": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "requeued_after_worker_rate_limit": 0,
                }
            )
        )
        return

    processed = 0
    succeeded = 0
    failed = 0
    requeued_after_worker_rate_limit = 0

    for idx, job in enumerate(jobs):
        if processed >= MAX_API_CALLS:
            log("Reached max API call budget for this run")
            requeued_after_worker_rate_limit += requeue_unprocessed_jobs(
                jobs,
                idx,
                "worker_budget_exhausted",
                delay_seconds=60,
            )
            break

        try:
            ok, message = process_job(access_token, job)
            processed += 1
            if ok:
                succeeded += 1
                log(f"detail ok for {message}")
            else:
                failed += 1
                mark_job_failed(job, message)
                log(f"detail failed for {job['source_listing_id']}: {message}")
        except WorkerRateLimitError as exc:
            processed += 1
            failed += 1
            mark_job_failed(job, exc.message, delay_seconds=exc.sleep_seconds)
            log(
                f"Worker-wide rate limit hit on {job['source_listing_id']}; "
                f"requeueing remaining jobs for {exc.sleep_seconds}s"
            )
            requeued_after_worker_rate_limit += requeue_unprocessed_jobs(
                jobs,
                idx + 1,
                "worker_rate_limited",
                delay_seconds=exc.sleep_seconds,
            )
            break
        except Exception as exc:
            processed += 1
            failed += 1
            err = str(exc)
            mark_job_failed(job, err)
            log(f"detail failed for {job['source_listing_id']}: {err}")

    log(
        json.dumps(
            {
                "released_stale_jobs": released,
                "claimed": len(jobs),
                "processed": processed,
                "succeeded": succeeded,
                "failed": failed,
                "requeued_after_worker_rate_limit": requeued_after_worker_rate_limit,
            }
        )
    )


if __name__ == "__main__":
    main()