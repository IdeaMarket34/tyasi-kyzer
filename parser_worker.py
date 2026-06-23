import importlib.util
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
from supabase import create_client


load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
PARSER_BATCH_SIZE = int(os.environ.get("PARSER_BATCH_SIZE", "50"))
BASE_DIR = Path(__file__).resolve().parent
CHARIZARD_REWRITE_PATH = BASE_DIR / "charizard_ingest_rewrite.py"

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_rewrite_module(module_path: str):
    path = Path(module_path)
    if not path.exists():
        raise FileNotFoundError(f"rewrite module not found: {module_path}")
    spec = importlib.util.spec_from_file_location("charizard_ingest_rewrite_runtime", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load module spec from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rewrite = load_rewrite_module(CHARIZARD_REWRITE_PATH)
map_item_to_bundle = rewrite.map_item_to_bundle


def load_detail_events(limit: int = PARSER_BATCH_SIZE) -> List[dict]:
    result = (
        supabase.table("raw_market_events")
        .select("id,source,source_listing_id,payload_json,observed_at")
        .eq("event_type", "detail")
        .order("observed_at", desc=False)
        .limit(limit)
        .execute()
    )
    return result.data or []


def get_market_listing_id(source: str, source_listing_id: str) -> Optional[int]:
    result = (
        supabase.table("market_listings")
        .select("id")
        .eq("source", source)
        .eq("source_listing_id", source_listing_id)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return rows[0]["id"] if rows else None


def ensure_market_listing(bundle: dict) -> Optional[int]:
    row = dict(bundle["market_listing_row"])
    row.setdefault("first_seen_at", utc_now())
    row.setdefault("last_detail_refresh_at", utc_now())
    row.setdefault("listing_status", "active")
    row["current_price_value"] = row.get("price_value")
    row["current_price_currency"] = "USD"

    detail_payload = row.get("raw_payload") or {}
    image = detail_payload.get("image") or {}
    seller = detail_payload.get("seller") or {}

    row["primary_image_url"] = image.get("imageUrl") or row.get("primary_image_url")
    row["seller_name"] = seller.get("username") or row.get("seller_name")
    row["seller_id"] = seller.get("username") or row.get("seller_id")
    row["item_location"] = (detail_payload.get("itemLocation") or {}).get("country") or row.get("item_location")

    supabase.table("market_listings").upsert(row, on_conflict="source_listing_id").execute()
    return get_market_listing_id(row["source"], row["source_listing_id"])


def insert_listing_history(market_listing_id: int, bundle: dict, observed_at: str) -> None:
    market_row = bundle["market_listing_row"]
    payload = market_row.get("raw_payload") or {}
    price = payload.get("price") or {}
    supabase.table("listing_history").insert({
        "market_listing_id": market_listing_id,
        "source": market_row["source"],
        "source_listing_id": market_row["source_listing_id"],
        "observed_at": observed_at,
        "listing_status": market_row.get("listing_status", "active"),
        "price_value": float(price["value"]) if price.get("value") else market_row.get("price_value"),
        "price_currency": price.get("currency", "USD"),
        "shipping_value": None,
        "condition_text": market_row.get("condition_text"),
        "title": market_row.get("raw_title"),
        "payload_json": payload,
    }).execute()


def upsert_listing_parse(market_listing_id: int, bundle: dict) -> None:
    parse_row = dict(bundle["listing_parse_row"])
    parse_row["market_listing_id"] = market_listing_id
    if "parser_notes" in parse_row:
        parse_row["parser_notes"] = parse_row["parser_notes"]
    supabase.table("listing_parses").upsert(parse_row, on_conflict="market_listing_id").execute()


def upsert_listing_card_match(market_listing_id: int, bundle: dict) -> None:
    parse_row = bundle["listing_parse_row"]
    matched_card_id = parse_row.get("matched_card_id")
    if not matched_card_id:
        return
    evidence = {
        "normalized_item_key": parse_row.get("normalized_item_key"),
        "set_guess": parse_row.get("set_guess"),
        "card_number_norm": parse_row.get("card_number_norm"),
        "promo_code_guess": parse_row.get("promo_code_guess"),
    }
    supabase.table("listing_card_matches").upsert({
        "market_listing_id": market_listing_id,
        "pokemon_card_id": matched_card_id,
        "match_method": "parser_worker",
        "match_confidence": parse_row.get("match_confidence"),
        "evidence_json": evidence,
        "updated_at": utc_now(),
    }, on_conflict="market_listing_id").execute()


def upsert_seller_profile(bundle: dict) -> None:
    payload = bundle["market_listing_row"].get("raw_payload") or {}
    seller = payload.get("seller") or {}
    seller_name = seller.get("username")
    if not seller_name:
        return
    supabase.table("seller_profiles").upsert({
        "source": bundle["market_listing_row"]["source"],
        "seller_id": seller_name,
        "seller_name": seller_name,
        "seller_profile_json": seller,
        "last_seen_at": utc_now(),
    }, on_conflict="source,seller_id").execute()


def replace_listing_images(market_listing_id: int, source_listing_id: str, bundle: dict) -> None:
    payload = bundle["market_listing_row"].get("raw_payload") or {}
    images = []
    primary = (payload.get("image") or {}).get("imageUrl")
    if primary:
        images.append(primary)
    for image_row in payload.get("additionalImages") or []:
        url = image_row.get("imageUrl")
        if url and url not in images:
            images.append(url)

    if not images:
        return

    existing = (
        supabase.table("listing_images")
        .select("id,image_url")
        .eq("source_listing_id", source_listing_id)
        .execute()
    )
    existing_urls = {row["image_url"] for row in (existing.data or [])}

    inserts = []
    for idx, url in enumerate(images):
        if url in existing_urls:
            continue
        inserts.append({
            "market_listing_id": market_listing_id,
            "source_listing_id": source_listing_id,
            "image_url": url,
            "ordinal": idx,
            "is_primary": idx == 0,
        })
    if inserts:
        supabase.table("listing_images").insert(inserts).execute()


def maybe_insert_sold_comp(bundle: dict) -> None:
    sold_comp = dict(bundle["sold_comp_row"])
    existing = (
        supabase.table("sold_comps")
        .select("external_comp_id")
        .eq("source", sold_comp["source"])
        .eq("external_comp_id", sold_comp["external_comp_id"])
        .limit(1)
        .execute()
    )
    if existing.data:
        return
    supabase.table("sold_comps").insert(sold_comp).execute()


def mark_jobs_done(source_listing_id: str) -> None:
    supabase.table("enrichment_jobs").update({
        "status": "done",
        "updated_at": utc_now(),
    }).eq("source", "ebay").eq("source_listing_id", source_listing_id).eq("job_type", "detail_fetch").execute()


def process_event(row: dict) -> Tuple[bool, str]:
    payload = row["payload_json"] or {}
    item = payload
    detail = payload
    bundle = map_item_to_bundle(item, detail)
    market_listing_id = ensure_market_listing(bundle)
    if not market_listing_id:
        return False, f"market listing missing for {row['source_listing_id']}"

    insert_listing_history(market_listing_id, bundle, row["observed_at"])
    upsert_listing_parse(market_listing_id, bundle)
    upsert_listing_card_match(market_listing_id, bundle)
    upsert_seller_profile(bundle)
    replace_listing_images(market_listing_id, row["source_listing_id"], bundle)
    # maybe_insert_sold_comp(bundle)
    mark_jobs_done(row["source_listing_id"])
    return True, row["source_listing_id"]


def main() -> None:
    rows = load_detail_events()
    processed = 0
    failed: List[str] = []
    for row in rows:
        try:
            ok, msg = process_event(row)
            if ok:
                processed += 1
            else:
                failed.append(msg)
        except Exception as e:
            failed.append(f"{row.get('source_listing_id')}: {e}")
    print({
        "detail_rows_seen": len(rows),
        "processed": processed,
        "failed": len(failed),
        "failures": failed[:10],
    })


if __name__ == "__main__":
    main()
