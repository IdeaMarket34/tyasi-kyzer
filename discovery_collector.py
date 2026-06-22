import base64
import hashlib
import json
import os
import time
from datetime import datetime, timezone
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

MAX_SEARCH_CALLS_PER_RUN = int(os.environ.get("MAX_SEARCH_CALLS_PER_RUN", "250"))
PAGE_SIZE = min(int(os.environ.get("DISCOVERY_PAGE_SIZE", "50")), 200)
MAX_PAGES_PER_PLAN = int(os.environ.get("MAX_PAGES_PER_PLAN", "5"))
SEARCH_PLAN_LIMIT = int(os.environ.get("SEARCH_PLAN_LIMIT", "50"))

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

EBAY_ACCESS_TOKEN_CACHE: Optional[str] = None
HTTP_SESSION = requests.Session()


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

    for attempt in range(1, 6):
        log(f"Browse search attempt {attempt}: q={query_text!r} offset={offset} params={params}")
        response = HTTP_SESSION.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers=get_headers(),
            params=params,
            timeout=30,
        )

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            wait_s = int(retry_after) if retry_after else min(2 ** attempt, 60)
            last_error = f"429 rate limit; retrying in {wait_s}s"
            log(last_error)
            time.sleep(wait_s)
            continue

        if response.status_code >= 400:
            body_preview = response.text[:1000]
            raise RuntimeError(
                f"Browse search failed ({response.status_code}) for query={query_text} "
                f"offset={offset} params={params} response={body_preview}"
            )

        return response.json(), 1

    raise RuntimeError(
        f"search failed after retries for query={query_text} filters={filter_json} last_error={last_error}"
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
    rows = []
    now_ts = utc_now()

    for item in items:
        price = item.get("price") or {}
        rows.append(
            {
                "source": source,
                "source_listing_id": item.get("itemId"),
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
                "raw_payload": item,
            }
        )

    for i in range(0, len(rows), 200):
        chunk = rows[i : i + 200]
        supabase.table("market_listings").upsert(chunk, on_conflict="source_listing_id").execute()


def process_plan(plan: dict, remaining_budget: int) -> Tuple[int, int, int, int]:
    search_run_id = create_search_run(plan)
    api_calls_used = 0
    total_results = 0
    unique_count = 0
    duplicate_count = 0
    all_summary_events: List[dict] = []
    all_items: List[dict] = []
    queued_listing_ids: List[str] = []

    try:
        log(f"Processing plan {plan['id']} query={plan['query_text']!r} filter_json={plan.get('filter_json') or {}}")

        for page_index in range(MAX_PAGES_PER_PLAN):
            if api_calls_used >= remaining_budget:
                break

            offset = page_index * PAGE_SIZE
            payload, calls_used = search_browse(
                query_text=plan["query_text"],
                filter_json=plan.get("filter_json") or {},
                offset=offset,
                limit=PAGE_SIZE,
            )
            api_calls_used += calls_used

            items = payload.get("itemSummaries") or []
            if not items:
                log(f"No items returned for plan {plan['id']} at offset {offset}")
                break

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

        if all_summary_events:
            insert_raw_events(all_summary_events)

        if all_items:
            upsert_market_listing_summaries(plan["source"], all_items)

        queued_count = (
            queue_enrichment_jobs(plan["source"], sorted(set(queued_listing_ids)))
            if queued_listing_ids
            else 0
        )

        if search_run_id:
            finalize_search_run(
                search_run_id,
                {
                    "status": "completed",
                    "api_calls_used": api_calls_used,
                    "result_count": total_results,
                    "unique_item_count": unique_count,
                    "duplicate_item_count": duplicate_count,
                    "error_count": 0,
                },
            )

        mark_search_plan_run(plan["id"], total_results)
        log(
            f"Completed plan {plan['id']}: calls={api_calls_used} "
            f"results={total_results} unique={unique_count} queued={queued_count}"
        )
        return api_calls_used, total_results, unique_count, queued_count

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
        return api_calls_used, total_results, unique_count, 0


def main() -> None:
    log("Starting discovery collector...")
    plans = load_active_search_plans()
    log(f"Loaded {len(plans)} active plans")

    remaining_budget = MAX_SEARCH_CALLS_PER_RUN
    total_calls = 0
    total_results = 0
    total_unique = 0
    total_queued = 0

    for plan in plans:
        if remaining_budget <= 0:
            log("Search budget exhausted.")
            break

        calls_used, result_count, unique_count, queued_count = process_plan(plan, remaining_budget)
        remaining_budget -= calls_used
        total_calls += calls_used
        total_results += result_count
        total_unique += unique_count
        total_queued += queued_count

    log(
        json.dumps(
            {
                "plans_processed": len(plans),
                "api_calls_used": total_calls,
                "results_seen": total_results,
                "unique_or_changed": total_unique,
                "detail_jobs_queued": total_queued,
                "remaining_budget": remaining_budget,
            }
        )
    )


if __name__ == "__main__":
    main()