import base64
import hashlib
import json
import os
import time
from datetime import datetime, timezone, timedelta
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

MAX_SEARCH_CALLS_PER_RUN = int(os.environ.get("MAX_SEARCH_CALLS_PER_RUN", "120"))
PAGE_SIZE = min(int(os.environ.get("DISCOVERY_PAGE_SIZE", "200")), 200)
MAX_PAGES_PER_PLAN = int(os.environ.get("MAX_PAGES_PER_PLAN", "5"))
SEARCH_PLAN_LIMIT = int(os.environ.get("SEARCH_PLAN_LIMIT", "50"))

# Minimum seconds to sleep between successful eBay API calls.
# This is the primary 429 prevention — keeps us well under the per-minute rate limit.
# Increase if 429s persist; 0.5s = ~120 calls/min ceiling, 1.0s = ~60 calls/min.
INTER_REQUEST_DELAY_S = float(os.environ.get("INTER_REQUEST_DELAY_S", "0.5"))

# How many times to retry a single request after a 429 before giving up on the
# current plan. Keeping this low (2) avoids spending the entire run window in
# retry sleep loops when eBay is consistently rate-limiting.
MAX_429_RETRIES = int(os.environ.get("MAX_429_RETRIES", "2"))

# If a plan's last_success_at is within this many minutes, skip it this run.
# Prevents back-to-back manual triggers from re-fetching identical data.
# Set to 0 to disable (default off so existing behavior is unchanged).
PLAN_COOLDOWN_MINUTES = int(os.environ.get("PLAN_COOLDOWN_MINUTES", "120"))

# Minimum eBay seller feedback score required to ingest a listing.
# Listings from sellers below this threshold are dropped at collection time —
# they never enter raw_market_events, enrichment_jobs, or market_listings.
# 780 zero-feedback and 479 1–4-feedback listings were found in session #26.
# Set to 0 to disable filtering entirely.
MIN_SELLER_FEEDBACK_SCORE = int(os.environ.get("MIN_SELLER_FEEDBACK_SCORE", "5"))

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

EBAY_ACCESS_TOKEN_CACHE: Optional[str] = None
HTTP_SESSION = requests.Session()


class RateLimitAbort(Exception):
    """Raised when a plan should abort cleanly due to exhausted 429 retries.
    Caught by process_plan; does NOT propagate to main so other plans can continue."""
    pass


def log(message: str) -> None:
    print(message, flush=True)


def utc_now() -> str:
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
    response = HTTP_SESSION.post(token_url, headers=headers, data=data, timeout=30)
    response.raise_for_status()

    token = response.json()["access_token"]
    EBAY_ACCESS_TOKEN_CACHE = token
    log("eBay application token acquired.")
    return token


def get_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {get_ebay_access_token()}",
        "Accept": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": EBAY_MARKETPLACE_ID,
    }


def load_active_search_plans(limit: int = SEARCH_PLAN_LIMIT) -> List[dict]:
    result = (
        supabase.table("search_plans")
        .select("*")
        .eq("is_active", True)
        .order("priority")
        .limit(limit)
        .execute()
    )
    return result.data or []


def create_search_run(plan: dict) -> Optional[str]:
    inserted = (
        supabase.table("search_runs")
        .insert(
            {
                "search_plan_id": plan["id"],
                "source": plan["source"],
                "status": "running",
                "query_text": plan["query_text"],
                "filter_json": plan.get("filter_json") or {},
                "started_at": utc_now(),
            }
        )
        .execute()
    )
    rows = inserted.data or []
    return rows[0]["id"] if rows else None


def finalize_search_run(search_run_id: str, updates: dict) -> None:
    payload = {**updates, "finished_at": utc_now()}
    supabase.table("search_runs").update(payload).eq("id", search_run_id).execute()


def mark_search_plan_run(plan_id: str, result_count: int) -> None:
    supabase.table("search_plans").update(
        {
            "last_run_at": utc_now(),
            "last_success_at": utc_now(),
            "last_result_count": result_count,
        }
    ).eq("id", plan_id).execute()


def build_filter_param(filter_json: Dict[str, Any]) -> Optional[str]:
    filters: List[str] = []

    buying_options = filter_json.get("buyingOptions") or []
    if buying_options:
        filters.append("buyingOptions:{%s}" % "|".join(str(x) for x in buying_options))

    category_ids = filter_json.get("categoryIds") or []
    if category_ids:
        filters.append("categoryIds:{%s}" % "|".join(str(x) for x in category_ids))

    item_conditions = filter_json.get("conditions") or []
    if item_conditions:
        filters.append("conditions:{%s}" % "|".join(str(x) for x in item_conditions))

    return ",".join(filters) if filters else None


def search_browse(
    query_text: str,
    filter_json: Dict[str, Any],
    offset: int = 0,
    limit: int = 50,
) -> Tuple[dict, int]:
    params = {
        "q": query_text,
        "limit": limit,
        "offset": offset,
    }

    filter_param = build_filter_param(filter_json)
    if filter_param:
        params["filter"] = filter_param

    last_error: Optional[str] = None

    for attempt in range(1, MAX_429_RETRIES + 1):
        log(f"Browse search attempt {attempt}/{MAX_429_RETRIES}: q={query_text!r} offset={offset}")
        response = HTTP_SESSION.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers=get_headers(),
            params=params,
            timeout=30,
        )

        # Log rate-limit headers if present so we can track headroom
        rl_remaining = response.headers.get("X-RateLimit-Remaining")
        rl_limit = response.headers.get("X-RateLimit-Limit")
        if rl_remaining is not None:
            log(f"  eBay rate-limit: {rl_remaining}/{rl_limit} remaining")

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            wait_s = int(retry_after) if retry_after else min(2 ** attempt, 30)
            last_error = f"429 rate limit (attempt {attempt}/{MAX_429_RETRIES}); waiting {wait_s}s"
            log(last_error)
            time.sleep(wait_s)
            continue

        if response.status_code >= 400:
            body_preview = response.text[:1000]
            raise RuntimeError(
                f"Browse search failed ({response.status_code}) for query={query_text} "
                f"offset={offset} response={body_preview}"
            )

        # Successful response — apply inter-request pacing before returning
        if INTER_REQUEST_DELAY_S > 0:
            time.sleep(INTER_REQUEST_DELAY_S)

        return response.json(), 1

    # All retries exhausted on 429 — abort this plan cleanly rather than raising a
    # generic RuntimeError. process_plan will save partial results and move on.
    raise RateLimitAbort(
        f"429 retries exhausted for query={query_text} offset={offset}; "
        f"aborting plan to preserve budget. last_error={last_error}"
    )


def get_latest_event_hashes(source: str, listing_ids: List[str]) -> Dict[str, str]:
    latest: Dict[str, str] = {}
    for i in range(0, len(listing_ids), 200):
        chunk = listing_ids[i : i + 200]
        result = (
            supabase.table("raw_market_events")
            .select("source_listing_id,payload_hash,observed_at")
            .eq("source", source)
            .eq("event_type", "summary")
            .in_("source_listing_id", chunk)
            .order("observed_at", desc=True)
            .execute()
        )
        for row in result.data or []:
            listing_id = row["source_listing_id"]
            if listing_id not in latest:
                latest[listing_id] = row.get("payload_hash")
    return latest


def insert_raw_events(events: List[dict]) -> None:
    for i in range(0, len(events), 200):
        chunk = events[i : i + 200]
        supabase.table("raw_market_events").insert(chunk).execute()


def queue_enrichment_jobs(source: str, listing_ids: List[str], reason: str = "new_or_changed") -> int:
    inserted = 0
    for listing_id in listing_ids:
        payload = {
            "source": source,
            "source_listing_id": listing_id,
            "job_type": "detail_fetch",
            "reason": reason,
            "priority": 100,
            "status": "queued",
            "payload_json": {},
        }
        result = (
            supabase.table("enrichment_jobs")
            .upsert(payload, on_conflict="source,source_listing_id,job_type,reason")
            .execute()
        )
        if result.data:
            inserted += 1
    return inserted


def upsert_market_listing_summaries(source: str, items: List[dict]) -> None:
    now_ts = utc_now()
    by_id: Dict[str, dict] = {}

    for item in items:
        listing_id = item.get("itemId")
        if not listing_id:
            continue

        price = item.get("price") or {}

        # Extract image URL from summary — Browse API includes image.imageUrl in
        # item_summary/search results. Capturing it here gives near-100% image
        # coverage without burning detail-fetch API calls. Detail fetches may later
        # overwrite with the same (or slightly higher-res) URL — that's fine.
        image = item.get("image") or {}
        image_url = image.get("imageUrl")
        if not image_url:
            thumbnails = item.get("thumbnailImages") or []
            if thumbnails:
                image_url = (thumbnails[0] or {}).get("imageUrl")

        seller = item.get("seller") or {}
        row = {
            "source": source,
            "source_listing_id": listing_id,
            "raw_title": item.get("title"),
            "listing_url": item.get("itemWebUrl"),
            "price_value": float(price["value"]) if price.get("value") else None,
            "current_price_value": float(price["value"]) if price.get("value") else None,
            "current_price_currency": price.get("currency", "USD"),
            "condition_text": item.get("condition"),
            "listing_type": item.get("buyingOptions", [None])[0] if item.get("buyingOptions") else None,
            "listing_start": item.get("itemCreationDate"),
            "listing_end": item.get("itemEndDate"),
            "listing_status": "active",
            "last_seen_at": now_ts,
            "first_seen_at": now_ts,
            "primary_image_url": image_url,
            "seller_name": seller.get("username"),
            "raw_payload": item,
        }

        by_id[listing_id] = row

    rows = list(by_id.values())

    for i in range(0, len(rows), 200):
        chunk = rows[i : i + 200]
        supabase.table("market_listings").upsert(
            chunk,
            on_conflict="source_listing_id"
        ).execute()


def plan_is_on_cooldown(plan: dict) -> bool:
    """Return True if this plan ran successfully too recently and should be skipped."""
    if PLAN_COOLDOWN_MINUTES <= 0:
        return False
    last_success = plan.get("last_success_at")
    if not last_success:
        return False
    try:
        last_dt = datetime.fromisoformat(last_success.replace("Z", "+00:00"))
        age_minutes = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
        return age_minutes < PLAN_COOLDOWN_MINUTES
    except Exception:
        return False


def seller_passes_filter(item: dict) -> bool:
    """Return True if the listing's seller meets the minimum feedback threshold.

    Checks item["seller"]["feedbackScore"] from the Browse API summary payload.
    Items with no seller block or missing feedbackScore are treated as failing
    (conservative — unknown sellers are treated like zero-feedback sellers).
    Pass MIN_SELLER_FEEDBACK_SCORE=0 to disable entirely.
    """
    if MIN_SELLER_FEEDBACK_SCORE <= 0:
        return True
    seller = item.get("seller") or {}
    score = seller.get("feedbackScore")
    if score is None:
        return False
    return int(score) >= MIN_SELLER_FEEDBACK_SCORE


def process_plan(plan: dict, remaining_budget: int) -> Tuple[int, int, int, int, int]:
    search_run_id = create_search_run(plan)
    api_calls_used = 0
    total_results = 0
    unique_count = 0
    duplicate_count = 0
    filtered_count = 0
    all_summary_events: List[dict] = []
    all_items: List[dict] = []
    queued_listing_ids: List[str] = []
    rate_limited = False

    try:
        log(f"Processing plan {plan['id']} query={plan['query_text']!r}")

        for page_index in range(MAX_PAGES_PER_PLAN):
            if api_calls_used >= remaining_budget:
                log(f"Budget exhausted mid-plan {plan['id']}; saving partial results.")
                break

            offset = page_index * PAGE_SIZE

            try:
                payload, calls_used = search_browse(
                    query_text=plan["query_text"],
                    filter_json=plan.get("filter_json") or {},
                    offset=offset,
                    limit=PAGE_SIZE,
                )
            except RateLimitAbort as e:
                log(f"Rate limit abort on plan {plan['id']} page {page_index}: {e}")
                rate_limited = True
                break  # Save whatever we collected so far; don't propagate

            api_calls_used += calls_used

            items = payload.get("itemSummaries") or []
            if not items:
                log(f"No items returned for plan {plan['id']} at offset {offset}")
                break

            # Drop listings from low-feedback / brand-new sellers before any
            # processing — they never reach raw_market_events or market_listings.
            pre_filter = len(items)
            items = [item for item in items if seller_passes_filter(item)]
            page_filtered = pre_filter - len(items)
            if page_filtered:
                log(
                    f"  Seller filter: dropped {page_filtered}/{pre_filter} items "
                    f"(min_feedback={MIN_SELLER_FEEDBACK_SCORE}, page {page_index})"
                )
            filtered_count += page_filtered

            total_results += len(items)
            all_items.extend(items)

            listing_ids = [item.get("itemId") for item in items if item.get("itemId")]
            latest_hashes = get_latest_event_hashes(plan["source"], listing_ids)

            for item in items:
                listing_id = item.get("itemId")
                if not listing_id:
                    continue

                current_hash = payload_hash(item)
                prior_hash = latest_hashes.get(listing_id)

                if prior_hash == current_hash:
                    duplicate_count += 1
                    continue

                unique_count += 1
                queued_listing_ids.append(listing_id)
                all_summary_events.append(
                    {
                        "source": plan["source"],
                        "source_listing_id": listing_id,
                        "event_type": "summary",
                        "observed_at": utc_now(),
                        "search_plan_id": plan["id"],
                        "search_run_id": search_run_id,
                        "payload_hash": current_hash,
                        "payload_json": item,
                    }
                )

            if len(items) < PAGE_SIZE:
                break

        # Persist whatever we collected, even if rate-limited mid-run
        if all_summary_events:
            insert_raw_events(all_summary_events)

        if all_items:
            upsert_market_listing_summaries(plan["source"], all_items)

        queued_count = (
            queue_enrichment_jobs(plan["source"], sorted(set(queued_listing_ids)))
            if queued_listing_ids
            else 0
        )

        final_status = "rate_limited_partial" if rate_limited else "completed"

        if search_run_id:
            finalize_search_run(
                search_run_id,
                {
                    "status": final_status,
                    "api_calls_used": api_calls_used,
                    "result_count": total_results,
                    "unique_item_count": unique_count,
                    "duplicate_item_count": duplicate_count,
                    "filtered_seller_count": filtered_count,
                    "error_count": 0,
                },
            )

        if not rate_limited:
            mark_search_plan_run(plan["id"], total_results)

        log(
            f"Plan {plan['id']} {final_status}: calls={api_calls_used} "
            f"results={total_results} unique={unique_count} "
            f"filtered_sellers={filtered_count} queued={queued_count}"
        )
        return api_calls_used, total_results, unique_count, queued_count, filtered_count

    except Exception as e:
        log(f"Plan failed {plan['id']}: {e}")
        if search_run_id:
            finalize_search_run(
                search_run_id,
                {
                    "status": "failed",
                    "api_calls_used": api_calls_used,
                    "result_count": total_results,
                    "unique_item_count": unique_count,
                    "duplicate_item_count": duplicate_count,
                    "error_count": 1,
                    "error_message": str(e),
                },
            )
        return api_calls_used, total_results, unique_count, 0, 0


def main() -> None:
    log(
        f"Starting discovery collector | "
        f"inter_request_delay={INTER_REQUEST_DELAY_S}s "
        f"max_429_retries={MAX_429_RETRIES} "
        f"plan_cooldown={PLAN_COOLDOWN_MINUTES}min "
        f"budget={MAX_SEARCH_CALLS_PER_RUN}"
    )
    plans = load_active_search_plans()
    log(f"Loaded {len(plans)} active plans")

    remaining_budget = MAX_SEARCH_CALLS_PER_RUN
    total_calls = 0
    total_results = 0
    total_unique = 0
    total_queued = 0
    total_filtered = 0
    plans_skipped = 0

    for plan in plans:
        if remaining_budget <= 0:
            log("Search budget exhausted.")
            break

        if plan_is_on_cooldown(plan):
            last_success = plan.get("last_success_at", "unknown")
            log(f"Skipping plan {plan['id']} (cooldown: last success {last_success})")
            plans_skipped += 1
            continue

        calls_used, result_count, unique_count, queued_count, filtered_count = process_plan(plan, remaining_budget)
        remaining_budget -= calls_used
        total_calls += calls_used
        total_results += result_count
        total_unique += unique_count
        total_queued += queued_count
        total_filtered += filtered_count

    log(
        json.dumps(
            {
                "plans_processed": len(plans) - plans_skipped,
                "plans_skipped_cooldown": plans_skipped,
                "api_calls_used": total_calls,
                "results_seen": total_results,
                "unique_or_changed": total_unique,
                "seller_filtered": total_filtered,
                "detail_jobs_queued": total_queued,
                "remaining_budget": remaining_budget,
            }
        )
    )


if __name__ == "__main__":
    main()