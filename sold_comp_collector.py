import os
from datetime import datetime, timezone
from typing import Dict, List

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
SOLD_PROVIDER = os.environ.get("SOLD_PROVIDER", "soldcomps")
SOLD_BATCH_SIZE = int(os.environ.get("SOLD_BATCH_SIZE", "10"))

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_targets(limit: int = SOLD_BATCH_SIZE) -> List[dict]:
    result = (
        supabase.table("sold_search_targets")
        .select("id,query_text,priority,pokemon_card_id,normalized_item_key")
        .eq("enabled", True)
        .order("priority", desc=False)
        .order("last_run_at", desc=False)
        .limit(limit)
        .execute()
    )
    return result.data or []


def fetch_sold_results(provider: str, query_text: str, limit: int = 100) -> List[Dict]:
    if provider == "soldcomps":
        return fetch_sold_results_soldcomps(query_text, limit=limit)
    raise ValueError(f"unsupported sold provider: {provider}")


def fetch_sold_results_soldcomps(query_text: str, limit: int = 100) -> List[Dict]:
    raise NotImplementedError("wire SoldComps HTTP call here")


def insert_raw_rows(rows: List[Dict]) -> int:
    if not rows:
        return 0

    inserted = 0
    for row in rows:
        payload = {
            "provider": row["provider"],
            "provider_record_id": row["provider_record_id"],
            "search_query": row["search_query"],
            "title": row["title"],
            "item_web_url": row.get("item_web_url"),
            "sold_at": row.get("sold_at"),
            "sold_price_value": row.get("sold_price_value"),
            "sold_price_currency": row.get("sold_price_currency"),
            "shipping_value": row.get("shipping_value"),
            "condition_text": row.get("condition_text"),
            "listing_format": row.get("listing_format"),
            "seller_name": row.get("seller_name"),
            "quantity_sold": row.get("quantity_sold"),
            "raw_json": row.get("raw_json") or {},
        }
        result = (
            supabase.table("sold_comps_raw")
            .upsert(payload, on_conflict="provider,provider_record_id")
            .execute()
        )
        if result.data:
            inserted += 1
    return inserted


def mark_target_success(target_id: int, result_count: int) -> None:
    now = utc_now()
    supabase.table("sold_search_targets").update({
        "last_run_at": now,
        "last_success_at": now,
        "last_result_count": result_count,
        "last_error": None,
        "updated_at": now,
    }).eq("id", target_id).execute()


def mark_target_error(target_id: int, error_text: str) -> None:
    now = utc_now()
    supabase.table("sold_search_targets").update({
        "last_run_at": now,
        "last_error": error_text[:1000],
        "updated_at": now,
    }).eq("id", target_id).execute()


def main() -> None:
    targets = load_targets()
    print({"targets_loaded": len(targets), "provider": SOLD_PROVIDER})

    for target in targets:
        try:
            rows = fetch_sold_results(SOLD_PROVIDER, target["query_text"], limit=100)
            inserted = insert_raw_rows(rows)
            mark_target_success(target["id"], len(rows))
            print({
                "target_id": target["id"],
                "query_text": target["query_text"],
                "results": len(rows),
                "inserted": inserted,
            })
        except Exception as e:
            mark_target_error(target["id"], str(e))
            print({
                "target_id": target["id"],
                "query_text": target["query_text"],
                "error": str(e),
            })


if __name__ == "__main__":
    main()
