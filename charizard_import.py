import base64
import os
from datetime import datetime, timezone
from uuid import uuid4

import requests
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

EBAY_CLIENT_ID = os.environ["EBAY_CLIENT_ID"]
EBAY_CLIENT_SECRET = os.environ["EBAY_CLIENT_SECRET"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def get_ebay_access_token() -> str:
    auth = base64.b64encode(f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()).decode()
    url = "https://api.ebay.com/identity/v1/oauth2/token"
    headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }
    response = requests.post(url, headers=headers, data=data, timeout=30)
    response.raise_for_status()
    return response.json()["access_token"]


def fetch_charizard_items(access_token: str):
    url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }
    params = {
        "q": "Charizard Pokemon card",
        "limit": "10",
    }
    response = requests.get(url, headers=headers, params=params, timeout=30)
    response.raise_for_status()
    return response.json().get("itemSummaries", [])


def normalize_charizard_key(title: str) -> str:
    t = title.lower()

    if "charizard" not in t:
        return "other_non_charizard"

    if "base set" in t and "1st edition" in t:
        if "psa 8" in t:
            return "charizard_base_1st_psa_8"
        if "psa 9" in t:
            return "charizard_base_1st_psa_9"
        if "psa 10" in t:
            return "charizard_base_1st_psa_10"
        return "charizard_base_1st_other"

    if "base set" in t and "shadowless" in t:
        if "psa 8" in t:
            return "charizard_base_shadowless_psa_8"
        if "psa 9" in t:
            return "charizard_base_shadowless_psa_9"
        if "psa 10" in t:
            return "charizard_base_shadowless_psa_10"
        return "charizard_base_shadowless_raw"

    if "base set" in t:
        if "psa 8" in t:
            return "charizard_base_unlimited_psa_8"
        if "psa 9" in t:
            return "charizard_base_unlimited_psa_9"
        if "psa 10" in t:
            return "charizard_base_unlimited_psa_10"
        return "charizard_base_unlimited_raw"

    if "151" in t or "sv151" in t:
        return "charizard_sv151"

    if "shining charizard" in t:
        return "charizard_shining"

    return "charizard_other_unclassified"


def extract_grade_company(title: str):
    t = title.lower()
    if "psa" in t:
        return "PSA"
    if "bgs" in t or "beckett" in t:
        return "BGS"
    if "cgc" in t:
        return "CGC"
    return None


def extract_grade_value(title: str):
    t = title.lower()
    for grade in ["10", "9", "8", "7", "6", "5", "4", "3", "2", "1"]:
        if f"psa {grade}" in t or f"bgs {grade}" in t or f"cgc {grade}" in t:
            return grade
    return None


def map_item_to_sold_comp(item: dict) -> dict:
    now_ts = datetime.now(timezone.utc).isoformat()

    title = item.get("title", "")
    price = item.get("price", {}) or {}
    price_value = float(price.get("value")) if price.get("value") else None
    currency = price.get("currency", "USD")
    item_id = item.get("itemId")
    item_url = item.get("itemWebUrl")

    return {
        "id": str(uuid4()),
        "normalized_item_key": normalize_charizard_key(title),
        "source": "ebay_browse",
        "title": title,
        "sold_price_value": price_value,
        "sold_price_currency": currency,
        "shipping_value": None,
        "condition_text": None,
        "sold_at": now_ts,
        "item_web_url": item_url,
        "raw_json": item,
        "created_at": now_ts,
        "source_tier": 2,
        "source_run_id": "ebay_browse_charizard_sample",
        "external_comp_id": item_id,
        "search_query": "Charizard Pokemon card",
        "sold_price": price_value,
        "shipping_price": None,
        "currency": currency,
        "comp_window_label": None,
        "grade_company": extract_grade_company(title),
        "grade_value": extract_grade_value(title),
        "listing_type": ",".join(item.get("buyingOptions", [])) if item.get("buyingOptions") else None,
        "confidence_grade": None,
        "is_valid_comp": True,
        "exclusion_reason": None,
        "updated_at": now_ts
    }


def insert_rows(rows):
    if not rows:
        return None

    external_ids = [row["external_comp_id"] for row in rows if row.get("external_comp_id")]

    existing = (
        supabase.table("sold_comps")
        .select("external_comp_id")
        .eq("source", "ebay_browse")
        .in_("external_comp_id", external_ids)
        .execute()
    )

    existing_ids = {row["external_comp_id"] for row in existing.data} if existing.data else set()

    new_rows = [row for row in rows if row.get("external_comp_id") not in existing_ids]

    print(f"Skipping {len(rows) - len(new_rows)} duplicate rows...")

    if not new_rows:
        print("No new rows to insert.")
        return None

    return supabase.table("sold_comps").insert(new_rows).execute()

def main():
    print("Getting eBay token...")
    token = get_ebay_access_token()

    print("Fetching Charizard items from eBay...")
    items = fetch_charizard_items(token)

    if not items:
        print("No items returned from eBay.")
        return

    rows = [map_item_to_sold_comp(item) for item in items]

    print(f"Inserting {len(rows)} rows into sold_comps...")
    result = insert_rows(rows)

    print("Done.")
    print(result)


if __name__ == "__main__":
    main()
