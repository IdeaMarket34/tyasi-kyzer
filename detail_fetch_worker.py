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
JOB_BATCH_SIZE = int(os.environ.get("JOB_BATCH_SIZE", "50"))
MAX_API_CALLS = int(os.environ.get("MAX_API_CALLS", "50"))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "30"))
LOCK_STALE_MINUTES = int(os.environ.get("LOCK_STALE_MINUTES", "30"))
WORKER_RATE_LIMIT_BACKOFF_SECONDS = int(os.environ.get("WORKER_RATE_LIMIT_BACKOFF_SECONDS", "300"))
# Legacy per-script daily ceiling. Superseded by EBAY_SHARED_DAILY_CAP (see
# below), which is checked against actual eBay-side call volume across ALL
# scripts (discovery_collector.py, detail_fetch_worker.py,
# backfill_sold_listings.py) via the ebay_api_call_log table. Kept around as
# a secondary per-script sanity ceiling, but no longer the primary guard —
# session #40 found two scripts independently enforcing ~1500/day with zero
# awareness of each other or of real eBay-side 429s, which is how the
# rate-limit incident happened despite both "budgets" individually looking fine.
DAILY_DETAIL_BUDGET = int(os.environ.get("DAILY_DETAIL_BUDGET", "1500"))

# Shared, cross-script daily ceiling on REAL eBay API call attempts (every
# attempt, including ones that 429/500/timeout — not just successful jobs).
# Set comfortably under eBay's documented 5,000/day Browse API limit to leave
# margin for undercounting/timing races between concurrently-running scripts.
EBAY_SHARED_DAILY_CAP = int(os.environ.get("EBAY_SHARED_DAILY_CAP", "4500"))
SCRIPT_NAME = os.environ.get("EBAY_CALL_SCRIPT_NAME", "detail_fetch_worker")


class SharedBudgetExceeded(Exception):
    """Raised when the shared cross-script eBay call budget (see
    ebay_api_call_log / ebay_calls_today()) has been reached, BEFORE making
    another real HTTP call to eBay. Distinct from WorkerRateLimitError, which
    fires only after eBay itself has already returned a 429."""
    def __init__(self, calls_today: int) -> None:
        super().__init__(f"shared_ebay_budget_exceeded:{calls_today}/{EBAY_SHARED_DAILY_CAP}")
        self.calls_today = calls_today


def check_shared_ebay_budget() -> int:
    """Read-only check against the shared daily call counter. Call this
    immediately before every real HTTP attempt to eBay, in every script that
    talks to eBay. Raises SharedBudgetExceeded if at/over the cap."""
    result = supabase.rpc("ebay_calls_today", {}).execute()
    calls_today = result.data if isinstance(result.data, int) else int(result.data)
    if calls_today >= EBAY_SHARED_DAILY_CAP:
        raise SharedBudgetExceeded(calls_today)
    return calls_today


def log_ebay_call(outcome: str, status_code: Optional[int] = None, source_listing_id: Optional[str] = None) -> None:
    """Log a real eBay API call attempt (any outcome) to the shared cross-script
    log. Call this AFTER every real HTTP attempt, success or failure alike —
    this is what fixes the undercounting from session #40 (retries on
    429/500/timeout were real calls that never got tallied anywhere)."""
    try:
        supabase.table("ebay_api_call_log").insert(
            {
                "script": SCRIPT_NAME,
                "outcome": outcome,
                "status_code": status_code,
                "source_listing_id": source_listing_id,
            }
        ).execute()
    except Exception as exc:
        # Logging failures should never take down the actual work.
        log(f"warning: failed to log ebay call to shared budget table: {exc}")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

HTTP_SESSION = requests.Session()
EBAY_ACCESS_TOKEN_CACHE: Optional[str] = None


class WorkerRateLimitError(Exception):
    def __init__(self, sleep_seconds: int, message: str = "worker_rate_limited") -> None:
        super().__init__(message)
        self.sleep_seconds = sleep_seconds
        self.message = message


class ItemNotFoundError(Exception):
    """Raised when eBay returns 404 for an item — almost always means the
    listing sold/ended and was delisted. The qty==0 check in
    build_market_listing_patch only catches multi-quantity listings going
    out of stock; a single-card listing (the common case here) disappears
    from the API entirely once it sells, so 404 is the real "sold" signal.
    Previously this fell through to the generic >=400 handler and was
    treated as a worker failure — the job eventually went to status=failed
    after exhausting retries, but market_listings.listing_status was never
    updated, so sold listings stayed "active" forever (session #39 finding).
    """
    def __init__(self, source_listing_id: str) -> None:
        super().__init__(f"item_not_found:{source_listing_id}")
        self.source_listing_id = source_listing_id


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
        # Check the SHARED cross-script budget before making the call, not
        # just this script's own counter. This is what was missing in
        # session #40 — detail_fetch_worker and backfill_sold_listings each
        # had their own ~1500/day ceiling, completely blind to each other and
        # to discovery_collector's volume, so neither one individually
        # tripping its own limit prevented the combined real eBay call volume
        # from running into trouble.
        check_shared_ebay_budget()

        try:
            response = HTTP_SESSION.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            # Network-level failures (read timeout, connection reset, DNS
            # blip) never reach the status-code checks below, so they need
            # their own retry path. Previously these propagated immediately
            # on the first occurrence with no retry at all (session #39 —
            # found when the backfill script hit a transient read timeout
            # and the whole run died on it instead of retrying).
            log_ebay_call("network_error", source_listing_id=source_listing_id)
            if attempt == max_attempts:
                raise RuntimeError(f"network_error_after_retries: {exc}")
            log(f"Network error for {source_listing_id}: {exc}; attempt {attempt}/{max_attempts}; retrying in {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            sleep_for = int(retry_after) if retry_after and retry_after.isdigit() else delay
            last_retry_after = sleep_for
            log_ebay_call("429", status_code=429, source_listing_id=source_listing_id)
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
            log_ebay_call("5xx", status_code=response.status_code, source_listing_id=source_listing_id)
            if attempt == max_attempts:
                raise RuntimeError(f"http_{response.status_code}: {body}")
            log(f"Transient server error for {source_listing_id}: {response.status_code}; retrying in {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue

        if response.status_code == 404:
            log_ebay_call("404", status_code=404, source_listing_id=source_listing_id)
            raise ItemNotFoundError(source_listing_id)

        if response.status_code >= 400:
            body = response.text[:1000] if response.text else ""
            log_ebay_call("4xx", status_code=response.status_code, source_listing_id=source_listing_id)
            raise RuntimeError(f"http_{response.status_code}: {body}")

        log_ebay_call("success", status_code=response.status_code, source_listing_id=source_listing_id)
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


def mark_listing_ended(source: str, source_listing_id: str) -> None:
    now = utc_now_iso()
    (
        supabase.table("market_listings")
        .update(
            {
                "listing_status": "ended",
                "last_detail_refresh_at": now,
                "last_seen_at": now,
            }
        )
        .eq("source", source)
        .eq("source_listing_id", source_listing_id)
        .execute()
    )


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


def mark_job_failed(
    job: Dict[str, Any],
    error_text: str,
    delay_seconds: Optional[int] = None,
    is_rate_limit: bool = False,
) -> None:
    attempt_count = job.get("attempt_count") or 1
    max_attempts = job.get("max_attempts") or 5

    # Rate-limit errors are a quota issue, not a job failure. Don't count them
    # toward max_attempts — instead requeue with the delay and undo the attempt
    # increment that claim_jobs applied, so the job eventually gets a fair retry.
    if is_rate_limit:
        update_payload = {
            "status": "queued",
            "locked_at": None,
            "worker_id": WORKER_ID,
            "last_error": error_text[:1000],
            "attempt_count": max(0, attempt_count - 1),  # undo claim increment
            "updated_at": utc_now_iso(),
            "available_at": (
                datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
            ).isoformat() if delay_seconds is not None else utc_now_iso(),
        }
        (
            supabase.table("enrichment_jobs")
            .update(update_payload)
            .eq("id", job["id"])
            .execute()
        )
        return

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
        mark_job_failed(job, reason, delay_seconds=delay_seconds, is_rate_limit=True)
        requeued += 1
    return requeued


def process_job(access_token: str, job: Dict[str, Any]) -> Tuple[bool, str]:
    source = job["source"]
    source_listing_id = job["source_listing_id"]

    if source != "ebay":
        return False, f"unsupported_source:{source}"

    try:
        detail = fetch_item_detail(access_token, source_listing_id)
    except ItemNotFoundError:
        # 404 from eBay's item endpoint means the listing is gone — almost
        # always because it sold. Mark it ended rather than failing the job,
        # since this is a normal/expected lifecycle outcome, not an error.
        mark_listing_ended(source, source_listing_id)
        mark_job_succeeded(job["id"])
        return True, f"{source_listing_id}:marked_ended_404"

    upsert_raw_market_event(source, source_listing_id, detail)
    patch = build_market_listing_patch(detail)
    update_market_listing(source, source_listing_id, patch)
    mark_job_succeeded(job["id"])
    return True, source_listing_id


def count_todays_completed_jobs() -> int:
    """Return the number of detail_fetch jobs completed so far today (UTC)."""
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()
    result = (
        supabase.table("enrichment_jobs")
        .select("id", count="exact")
        .eq("job_type", "detail_fetch")
        .eq("status", "done")
        .gte("updated_at", today_start)
        .execute()
    )
    return result.count or 0


def main() -> None:
    log("Starting detail fetch worker...")

    todays_count = count_todays_completed_jobs()
    log(f"Daily budget check: {todays_count}/{DAILY_DETAIL_BUDGET} detail jobs completed today")
    if todays_count >= DAILY_DETAIL_BUDGET:
        log(
            f"Daily detail budget exhausted ({todays_count}/{DAILY_DETAIL_BUDGET}). "
            "Exiting to protect eBay API quota. Will resume after UTC midnight reset."
        )
        return

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
        except SharedBudgetExceeded as exc:
            # Shared cross-script budget is exhausted — not an eBay-side 429,
            # just us proactively stopping before making another real call.
            # Requeue everything left for a fresh shot tomorrow (UTC).
            log(
                f"Shared eBay daily budget reached ({exc.calls_today}/{EBAY_SHARED_DAILY_CAP}); "
                f"stopping before job {job['source_listing_id']} and requeueing the rest."
            )
            requeued_after_worker_rate_limit += requeue_unprocessed_jobs(
                jobs,
                idx,
                "shared_budget_exceeded",
                delay_seconds=3600,
            )
            break
        except WorkerRateLimitError as exc:
            processed += 1
            failed += 1
            mark_job_failed(job, exc.message, delay_seconds=exc.sleep_seconds, is_rate_limit=True)
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