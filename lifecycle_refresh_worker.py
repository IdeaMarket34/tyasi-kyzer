
import os
from datetime import datetime, timezone, timedelta
from typing import List

from dotenv import load_dotenv
from supabase import create_client


load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
REFRESH_BATCH_SIZE = int(os.environ.get("REFRESH_BATCH_SIZE", "100"))
STALE_HOURS = int(os.environ.get("STALE_HOURS", "12"))

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_stale_active_listings(limit: int = REFRESH_BATCH_SIZE) -> List[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=STALE_HOURS)
    result = (
        supabase.table("market_listings")
        .select("id,source,source_listing_id,listing_status,last_detail_refresh_at")
        .eq("source", "ebay")
        .eq("listing_status", "active")
        .or_(f"last_detail_refresh_at.is.null,last_detail_refresh_at.lt.{cutoff.isoformat()}")
        .order("last_detail_refresh_at", desc=False)
        .limit(limit)
        .execute()
    )
    return result.data or []


def enqueue_refresh_jobs(listings: List[dict]) -> int:
    inserted = 0
    for row in listings:
        payload = {
            "source": row["source"],
            "source_listing_id": row["source_listing_id"],
            "job_type": "detail_fetch",
            "reason": "stale_refresh",
            "priority": 120,
            "status": "queued",
        }
        res = supabase.table("enrichment_jobs").upsert(
            payload,
            on_conflict="source,source_listing_id,job_type,reason",
        ).execute()
        if res.data:
            inserted += 1
    return inserted


def main() -> None:
    listings = get_stale_active_listings()
    queued = enqueue_refresh_jobs(listings)
    print({
        "stale_candidates": len(listings),
        "jobs_queued": queued,
    })


if __name__ == "__main__":
    main()
