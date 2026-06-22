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

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

HTTP_SESSION = requests.Session()
EBAY_ACCESS_TOKEN_CACHE: Optional[str] = None


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

    max_attempts = 5
    delay = 2

    for attempt in range(1, max_attempts + 1):
        response = HTTP_SESSION.get(url, headers=headers, timeout=REQUEST_TIMEOUT)

        if response.status_code == 429:
            if attempt == max_attempts:
                raise RuntimeError("http_429")
            retry_after = response.headers.get("Retry-After")
            sleep_for = int(retry_after) if retry_after and retry_after.isdigit() else delay
            log(f"Rate limited for {source_listing_id}; retrying in {sleep_for}s")
            time.sleep(sleep_for)
            delay = min(delay * 2, 60)
            continue

        if response.status_code >= 400:
            body = response.text[:1000] if response.text else ""
            raise RuntimeError(f"http_{response.status_code}: {body}")

        return response.json()

    raise RuntimeError("detail_fetch_failed")


def build_market_listing_patch(detail: Dict[str, Any]) -> Dict[str, Any]:
    shipping_value = None
    shipping_currency = None
    shipping_opts = detail.get("shippingOptions") or []
    if shipping_opts:
        cost = shipping_opts[0].get("shippingCost") or {}
        shipping_value = cost.get("value")
        shipping_currency = cost.get("currency")

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
        "shipping_currency": shipping_currency,
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


def mark_job_failed(job: Dict[str, Any], error_text: str) -> None:
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
        backoff_minutes = min(60, 2 ** max(1, attempt_count))
        update_payload["available_at"] = (
            datetime.now(timezone.utc) + timedelta(minutes=backoff_minutes)
        ).isoformat()

    (
        supabase.table("enrichment_jobs")
        .update(update_payload)
        .eq("id", job["id"])
        .execute()
    )


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
    access_token = get_ebay_access_token()

    log("Claiming jobs...")
    jobs = claim_jobs(JOB_BATCH_SIZE)
    log(f"Claimed {len(jobs)} detail jobs")

    if not jobs:
        log("No detail jobs available.")
        return

    processed = 0
    succeeded = 0
    failed = 0

    for job in jobs:
        if processed >= MAX_API_CALLS:
            log("Reached max API call budget for this run")
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
        except Exception as exc:
            processed += 1
            failed += 1
            err = str(exc)
            mark_job_failed(job, err)
            log(f"detail failed for {job['source_listing_id']}: {err}")

    log(
        json.dumps(
            {
                "claimed": len(jobs),
                "processed": processed,
                "succeeded": succeeded,
                "failed": failed,
            }
        )
    )


if __name__ == "__main__":
    main()