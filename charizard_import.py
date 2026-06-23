import base64
import os
import re
from datetime import datetime, timezone
from uuid import uuid4
from dataclasses import dataclass
from typing import Optional, Dict
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse
import time

import requests
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

EBAY_CLIENT_ID = os.environ["EBAY_CLIENT_ID"]
EBAY_CLIENT_SECRET = os.environ["EBAY_CLIENT_SECRET"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

PROMO_RE = re.compile(r'\b(?:swsh|svp|sm|xy|bw)\s*[-#]?\s*(\d{1,4})\b', re.I)
FRACTION_RE = re.compile(r'\b([a-z]{0,3}\d{1,4})\s*/\s*([a-z]{0,3}\d{1,4})\b', re.I)

def _normalize_card_part(part: str) -> str:
    part = (part or "").strip().upper()
    m = re.match(r'^([A-Z]{0,3})(\d{1,4})$', part)
    if not m:
        return part.lower()
    prefix = m.group(1)
    number = str(int(m.group(2)))
    return f"{prefix}{number}" if prefix else number

def extract_card_number(t: str):
    m = FRACTION_RE.search(t)
    if not m:
        return None, None, None, None

    raw_left = m.group(1)
    raw_right = m.group(2)

    left_norm = _normalize_card_part(raw_left)
    right_norm = _normalize_card_part(raw_right)

    raw = f"{raw_left}/{raw_right}"
    fraction_norm = f"{left_norm}/{right_norm}"

    return raw, left_norm, right_norm, fraction_norm

def normalize_card_number_from_aspect(card_number_raw: Optional[str]):
    if not card_number_raw:
        return None, None, None, None

    m = FRACTION_RE.search(card_number_raw)
    if not m:
        cleaned = card_number_raw.strip()
        return cleaned, None, None, None

    raw_left = m.group(1)
    raw_right = m.group(2)

    left_norm = _normalize_card_part(raw_left)
    right_norm = _normalize_card_part(raw_right)

    raw = f"{raw_left}/{raw_right}"
    fraction_norm = f"{left_norm}/{right_norm}"

    return raw, left_norm, right_norm, fraction_norm

GRADE_RE = re.compile(r'\b(psa|bgs|cgc|sgc)\s*(\d{1,2}(?:\.\d)?)\b', re.I)

JUNK_PATTERNS = [
    r'\bproxy\b',
    r'\bcustom\b',
    r'\bmetal card\b',
    r'\bgift\s*/?\s*display\b',
    r'\binspired\b',
    r'\bjumbo\b',
    r'\bcoin[s]?\b',
    r'\blot\b',
    r'\b\d+\s*pcs\b',
]

SET_ALIASES = {
    "obsidian flames": "obsidian_flames",
    "sv03": "obsidian_flames",

    "base set": "bs",
    "pokemon base set": "bs",
    "base": "bs",

    "team rocket": "tr",
    "rocket gang": "tr",

    "skyridge": "sk",

    "power keepers": "pk",

    "champion's path": "cpa",
    "champions path": "cpa",

    "brilliant stars": "brs",

    "burning shadows": "bus",

    "pokemon 151": "151",
    "sv151": "151",
}

@dataclass
class ParsedTitle:
    raw_title: str
    normalized_title: str
    pokemon_name: Optional[str]
    set_guess: Optional[str]
    card_number_guess: Optional[str]
    card_number_norm: Optional[str]       # left side only
    card_total_guess: Optional[str]       # right side only
    card_fraction_norm: Optional[str]     # full normalized fraction
    promo_code_guess: Optional[str]
    grade_company: Optional[str]
    grade_value: Optional[float]
    language_guess: Optional[str]
    variant_guess: Optional[str]
    is_junk: bool
    junk_reason: Optional[str]


def normalize_text(title: str) -> str:
    t = title.lower()
    t = re.sub(r'[^\w/#&+\-\s:.]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def detect_junk(t: str):
    for pat in JUNK_PATTERNS:
        if re.search(pat, t):
            return True, pat
    return False, None


def extract_promo_code(t: str):
    m = re.search(r'\b(swsh|svp|sm|xy|bw)\s*[-#]?\s*(\d{1,4})\b', t, re.I)
    if not m:
        return None
    prefix = m.group(1).lower()
    number = m.group(2).zfill(3)
    return f"{prefix}{number}"

def extract_card_number(t: str):
    m = FRACTION_RE.search(t)
    if not m:
        return None, None, None, None

    num = str(int(m.group(1)))
    total = str(int(m.group(2)))
    raw = f"{num}/{total}"
    fraction_norm = f"{num}/{total}"
    return raw, num, total, fraction_norm


def extract_grade(t: str):
    m = GRADE_RE.search(t)
    if not m:
        return None, None
    return m.group(1).upper(), float(m.group(2))


def extract_set_guess(t: str):
    for alias, set_key in SET_ALIASES.items():
        if alias in t:
            return set_key
    return None


def extract_language(t: str):
    if "japanese" in t or "jp" in t:
        return "ja"
    return "en"


def extract_variant(t: str):
    variant_patterns = [
        (r'\bvstar\b', "vstar"),
        (r'\bvmax\b', "vmax"),
        (r'\bgx\b', "gx"),
        (r'\bex\b', "ex"),
        (r'\bdark\b', "dark"),
        (r'\bshining\b', "shining"),
        (r'\breverse holo\b', "reverse_holo"),
        (r'\bholo\b', "holo"),
        (r'\bpromo\b', "promo"),
        (r'\bv\b', "v"),
    ]

    for pattern, value in variant_patterns:
        if re.search(pattern, t):
            return value
    return None

def parse_listing_title(title: str) -> ParsedTitle:
    t = normalize_text(title)
    is_junk, junk_reason = detect_junk(t)
    promo = extract_promo_code(t)
    card_raw, card_norm, card_total, card_fraction = extract_card_number(t)
    grade_company, grade_value = extract_grade(t)
    card_number_guess=card_raw,
    card_number_norm=card_norm,
    card_total_guess=card_total,
    card_fraction_norm=card_fraction,

    return ParsedTitle(
        raw_title=title,
        normalized_title=t,
        pokemon_name="charizard" if "charizard" in t else None,
        set_guess=extract_set_guess(t),
        card_number_guess=card_raw,
        card_number_norm=card_norm,
        card_number_guess: Optional[str]
        card_number_norm: Optional[str]      # numerator only, to match DB
        card_total_guess: Optional[str]
        card_fraction_norm: Optional[str]
        promo_code_guess=promo,
        grade_company=grade_company,
        grade_value=grade_value,
        language_guess=extract_language(t),
        variant_guess=extract_variant(t),
        is_junk=is_junk,
        junk_reason=junk_reason,
    )

def extract_aspects(detail: dict) -> Dict[str, Optional[str]]:
    """
    Extract structured fields from eBay Browse item detail.
    """
    aspects = detail.get("localizedAspects") or []
    result = {
        "set_name": None,
        "card_number_raw": None,
        "language": None,
        "game": None,
        "character": None,
        "rarity": None,
    }

    for a in aspects:
        name = (a.get("name") or "").strip()
        value = (a.get("value") or "").strip()

        if not name or not value:
            continue

        # Normalize common aspect names
        lname = name.lower()

        if lname == "set":
            result["set_name"] = value
        elif lname == "card number":
            result["card_number_raw"] = value
        elif lname == "language":
            result["language"] = value
        elif lname == "game":
            result["game"] = value
        elif lname == "character":
            result["character"] = value
        elif lname == "rarity":
            result["rarity"] = value

    return result

def normalize_card_number_from_aspect(card_number_raw: Optional[str]):
    if not card_number_raw:
        return None, None

    # Examples: "SV107/SV122", "21/82", "014/172"
    m = re.search(r'(\d{1,4})\s*/\s*(\d{1,4})', card_number_raw)
    if not m:
        return card_number_raw, None

    num = int(m.group(1))
    denom = int(m.group(2))
    return f"{num}/{denom}", f"{num}_{denom}"

def match_reference_card(parsed: ParsedTitle, aspects: Optional[Dict[str, Optional[str]]] = None):
    if not parsed.pokemon_name:
        return None

    aspects = aspects or {}
    language = parsed.language_guess or "en"

    set_guess = parsed.set_guess
    card_num = parsed.card_number_norm
    card_total = parsed.card_total_guess
    promo_code = parsed.promo_code_guess

    if not set_guess and aspects.get("set_name"):
        set_guess = extract_set_guess(normalize_text(aspects["set_name"]))

    if (not card_num or not card_total) and aspects.get("card_number_raw"):
        raw, num, total = normalize_card_number_from_aspect_v2(aspects["card_number_raw"])
        if num:
            card_num = num
        if total:
            card_total = total

    # 1) promo match
    if promo_code:
        prefix = re.match(r'^[a-z]+', promo_code)
        digits = re.search(r'(\d+)$', promo_code)
        if prefix and digits:
            promo_result = (
                supabase.table("pokemon_cards")
                .select("id,card_key,pokemon_name,set_id,card_number,card_number_norm,total_in_set,promo_prefix,language,metadata")
                .eq("pokemon_name", parsed.pokemon_name)
                .eq("promo_prefix", prefix.group(0).upper())
                .eq("card_number_norm", str(int(digits.group(1))))
                .eq("language", language)
                .limit(5)
                .execute()
            )
            if len(promo_result.data or []) == 1:
                return promo_result.data[0]

    # 2) set + numerator + denominator
    if set_guess and card_num and card_total:
        set_result = (
            supabase.table("pokemon_sets")
            .select("id,set_key,set_name")
            .eq("set_key", set_guess)
            .limit(1)
            .execute()
        )
        if set_result.data:
            set_id = set_result.data[0]["id"]
            card_result = (
                supabase.table("pokemon_cards")
                .select("id,card_key,pokemon_name,set_id,card_number,card_number_norm,total_in_set,promo_prefix,language,metadata")
                .eq("pokemon_name", parsed.pokemon_name)
                .eq("set_id", set_id)
                .eq("card_number_norm", card_num)
                .eq("total_in_set", card_total)
                .eq("language", language)
                .limit(5)
                .execute()
            )
            if len(card_result.data or []) == 1:
                return card_result.data[0]

    # 3) unique fraction across all Charizard cards
    if card_num and card_total:
        num_result = (
            supabase.table("pokemon_cards")
            .select("id,card_key,pokemon_name,set_id,card_number,card_number_norm,total_in_set,promo_prefix,language,metadata")
            .eq("pokemon_name", parsed.pokemon_name)
            .eq("card_number_norm", card_num)
            .eq("total_in_set", card_total)
            .eq("language", language)
            .limit(10)
            .execute()
        )
        candidates = num_result.data or []
        if len(candidates) == 1:
            return candidates[0]

    return None

def upsert_market_listing(item: dict) -> int:
    row = {
        "source": "ebay",
        "source_listing_id": item["itemId"],
        "raw_title": item.get("title"),
        "listing_url": item.get("itemWebUrl"),
        "price_value": float(item["price"]["value"]) if item.get("price") else None,
        "condition_text": item.get("condition"),
        "listing_type": item.get("buyingOptions", [None])[0] if item.get("buyingOptions") else None,
        "listing_start": item.get("itemCreationDate"),
        "listing_end": item.get("itemEndDate"),
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
        "raw_payload": item,
    }

    result = (
        supabase.table("market_listings")
        .upsert(row, on_conflict="source_listing_id")
        .execute()
    )

    return result.data[0]["id"]


def insert_listing_parse(market_listing_id: int, parsed: ParsedTitle):
    matched_card = match_reference_card(parsed)

    row = {
        "market_listing_id": market_listing_id,
        "parse_version": "v1_parser",
        "pokemon_name": parsed.pokemon_name,
        "set_guess": parsed.set_guess,
        "card_number_guess": parsed.card_number_guess,
        "card_number_norm": parsed.card_number_norm,
        "promo_code_guess": parsed.promo_code_guess,
        "variant_guess": parsed.variant_guess,
        "language_guess": parsed.language_guess,
        "grade_company": parsed.grade_company,
        "grade_value": parsed.grade_value,
        "is_junk": parsed.is_junk,
        "junk_reason": parsed.junk_reason,
        "match_confidence": 0.97 if matched_card else None,
        "matched_card_id": matched_card["id"] if matched_card else None,
        "normalized_item_key": None,
        "parser_notes": {
            "normalized_title": parsed.normalized_title
        },
    }

    return (
        supabase.table("listing_parses")
        .upsert(row, on_conflict="market_listing_id")
        .execute()
    )

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
    collected = []

    queries = [
        "Charizard",
        "Charizard Pokemon card",
        "Charizard ex",
        "Charizard promo",
        "Japanese Charizard",
        "Mega Charizard",
    ]

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }

    def run_query(query):
        url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
        offset = 0

        while True:
            params = {
                "q": query,
                "limit": 200,
                "offset": offset,
            }

            for attempt in range(1, 6):
                response = requests.get(url, headers=headers, params=params, timeout=30)

                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        wait_s = int(retry_after)
                    else:
                        wait_s = min(2 ** attempt, 60)

                    print(
                        f"Search rate-limited on query '{query}'. "
                        f"Waiting {wait_s}s before retry {attempt}/5..."
                    )
                    time.sleep(wait_s)
                    continue

                response.raise_for_status()
                data = response.json()
                break
            else:
                raise RuntimeError(f"Search failed after retries for query: {query}")

            items = data.get("itemSummaries", [])
            if not items:
                break

            collected.extend(items)

            if len(items) < 200:
                break

            offset += 200

    for query in queries:
        run_query(query)

    return collected

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


def map_item_to_sold_comp(item: dict, detail: Optional[dict] = None) -> dict:
    now_ts = datetime.now(timezone.utc).isoformat()

    title = item.get("title", "")
    # Structured aspects from item detail (if provided)
    aspect_data = extract_aspects(detail or item)
    aspect_set_name = aspect_data.get("set_name")
    aspect_card_number_raw = aspect_data.get("card_number_raw")
    aspect_language = aspect_data.get("language")
    card_number_guess_from_aspect, card_number_norm_from_aspect = normalize_card_number_from_aspect(aspect_card_number_raw)
    parsed = parse_listing_title(title)
    # Override / supplement parsed fields with structured aspects when available
    if aspect_set_name:
        parsed.set_guess = aspect_set_name

    if card_number_guess_from_aspect:
        parsed.card_number_guess = card_number_guess_from_aspect
    if card_number_norm_from_aspect:
        parsed.card_number_norm = card_number_norm_from_aspect

    if aspect_language:
        parsed.language_guess = aspect_language

    price = item.get("price", {}) or {}
    price_value = float(price.get("value")) if price.get("value") else None
    currency = price.get("currency", "USD")
    item_id = item.get("itemId")
    item_url = item.get("itemWebUrl")

    return {
        "parsed": parsed,
        "market_listing_row": {
            "source": "ebay",
            "source_listing_id": item_id,
            "raw_title": title,
            "listing_url": item_url,
            "price_value": price_value,
            "condition_text": item.get("condition"),
            "listing_type": item.get("buyingOptions", [None])[0] if item.get("buyingOptions") else None,
            "listing_start": item.get("itemCreationDate"),
            "listing_end": item.get("itemEndDate"),
            "last_seen_at": now_ts,
            "raw_payload": item,
        },
        "sold_comp_row": {
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
            "search_query": "Charizard multi-query import",
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
            "updated_at": now_ts,
        },
    }

import time


def chunk_list(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def insert_rows(rows):
    if not rows:
        return None

    external_ids = [row["external_comp_id"] for row in rows if row.get("external_comp_id")]
    existing_ids = set()

    for id_chunk in chunk_list(external_ids, 200):
        existing = (
            supabase.table("sold_comps")
            .select("external_comp_id")
            .eq("source", "ebay_browse")
            .in_("external_comp_id", id_chunk)
            .execute()
        )

        if existing.data:
            existing_ids.update(
                row["external_comp_id"]
                for row in existing.data
                if row.get("external_comp_id")
            )

    new_rows = [row for row in rows if row.get("external_comp_id") not in existing_ids]

    print(f"Skipping {len(rows) - len(new_rows)} duplicate rows...")

    if not new_rows:
        print("No new rows to insert.")
        return None

    inserted_total = 0
    last_result = None
    row_chunks = list(chunk_list(new_rows, 100))

    for chunk_index, row_chunk in enumerate(row_chunks, start=1):
        max_attempts = 3

        for attempt in range(1, max_attempts + 1):
            try:
                print(f"Inserting chunk {chunk_index}/{len(row_chunks)} with {len(row_chunk)} sold comps (attempt {attempt})...")
                last_result = supabase.table("sold_comps").insert(row_chunk).execute()
                inserted_total += len(row_chunk)
                break
            except Exception as e:
                print(f"Chunk {chunk_index} failed on attempt {attempt}: {e}")
                if attempt == max_attempts:
                    raise
                time.sleep(2 ** attempt)

    print(f"Inserted {inserted_total} new sold comps.")
    return last_result

def debug_parse_examples():
    examples = [
        "Charizard ex - 223/197 - SV03: Obsidian Flames SIR Near Mint",
        "Charizard V SWSH050 Promo Full Art NM-MT",
        "【NM-】Dark Charizard No. 006 Rocket Gang Holo Vintage Japanese Pokemon",
        "Charizard 3/108 Holo Rare 140 HP Stage 2 EX Power Keepers Pokemon TCG EN",
        "Charizard 146/144 Skyridge Holo",
    ]

    for title in examples:
        parsed = parse_listing_title(title)
        print("TITLE:", title)
        print("  normalized_title:", parsed.normalized_title)
        print("  pokemon_name:", parsed.pokemon_name)
        print("  set_guess:", parsed.set_guess)
        print("  card_number_guess:", parsed.card_number_guess)
        print("  card_number_norm:", parsed.card_number_norm)
        print("  promo_code_guess:", parsed.promo_code_guess)
        print("  grade_company:", parsed.grade_company)
        print("  grade_value:", parsed.grade_value)
        print("  language_guess:", parsed.language_guess)
        print("  variant_guess:", parsed.variant_guess)
        print("  is_junk:", parsed.is_junk, "junk_reason:", parsed.junk_reason)
        print()

def debug_save_one_example():
    item = {
        "itemId": f"debug-{uuid4()}",
        "title": "Charizard ex - 223/197 - SV03: Obsidian Flames SIR Near Mint",
        "itemWebUrl": "https://example.com/debug-listing",
        "price": {"value": "199.99"},
        "condition": "Near Mint or Better",
        "buyingOptions": ["FIXED_PRICE"],
        "itemCreationDate": datetime.now(timezone.utc).isoformat(),
        "itemEndDate": datetime.now(timezone.utc).isoformat(),
    }

    parsed = parse_listing_title(item["title"])
    market_listing_id = upsert_market_listing(item)
    insert_listing_parse(market_listing_id, parsed)

    print("saved market_listing_id:", market_listing_id)
    print("parsed title:", parsed)

def rematch_unmatched_parses(limit: int = 100):
    print(f"Rematching up to {limit} unmatched listing_parses rows...")

    result = (
        supabase.table("listing_parses")
        .select("id, market_listing_id, pokemon_name, set_guess, card_number_norm, language_guess")
        .is_("matched_card_id", None)
        .eq("is_junk", False)
        .limit(limit)
        .execute()
    )

    rows = result.data or []
    print(f"Found {len(rows)} unmatched rows")

    for row in rows:
        ml_id = row["market_listing_id"]
        lp_id = row["id"]

        # Fetch the full parse row in case we need normalized_title
        lp_full = (
            supabase.table("listing_parses")
            .select("*")
            .eq("id", lp_id)
            .limit(1)
            .execute()
        )
        if not lp_full.data:
            continue

        parsed = ParsedTitle(
            raw_title=lp_full.data[0].get("parser_notes", {}).get("normalized_title") or "",
            normalized_title=lp_full.data[0].get("parser_notes", {}).get("normalized_title") or "",
            pokemon_name=lp_full.data[0].get("pokemon_name"),
            set_guess=lp_full.data[0].get("set_guess"),
            card_number_guess=lp_full.data[0].get("card_number_guess"),
            card_number_norm=lp_full.data[0].get("card_number_norm"),
            promo_code_guess=lp_full.data[0].get("promo_code_guess"),
            grade_company=lp_full.data[0].get("grade_company"),
            grade_value=lp_full.data[0].get("grade_value"),
            language_guess=lp_full.data[0].get("language_guess"),
            variant_guess=lp_full.data[0].get("variant_guess"),
            is_junk=lp_full.data[0].get("is_junk"),
            junk_reason=lp_full.data[0].get("junk_reason"),
        )

        matched_card = match_reference_card(parsed)

        if matched_card:
            print(
                f"Matched parse {lp_id} (listing {ml_id}) "
                f"to card {matched_card['card_key']}"
            )

            (
                supabase.table("listing_parses")
                .update({
                    "matched_card_id": matched_card["id"],
                    "match_confidence": 0.97,
                })
                .eq("id", lp_id)
                .execute()
            )

import time


def upsert_market_listings_batch(rows, chunk_size=100):
    if not rows:
        return {}

    unique_rows = {}
    for row in rows:
        source_listing_id = row.get("source_listing_id")
        if source_listing_id:
            unique_rows[source_listing_id] = row

    deduped_rows = list(unique_rows.values())
    print(f"Upserting {len(deduped_rows)} market_listings rows...")

    row_chunks = list(chunk_list(deduped_rows, chunk_size))

    for chunk_index, row_chunk in enumerate(row_chunks, start=1):
        max_attempts = 3

        for attempt in range(1, max_attempts + 1):
            try:
                print(f"Upserting market_listings chunk {chunk_index}/{len(row_chunks)} with {len(row_chunk)} rows (attempt {attempt})...")
                supabase.table("market_listings").upsert(
                    row_chunk,
                    on_conflict="source_listing_id"
                ).execute()
                break
            except Exception as e:
                print(f"market_listings chunk {chunk_index} failed on attempt {attempt}: {e}")
                if attempt == max_attempts:
                    raise
                time.sleep(2 ** attempt)

    listing_id_map = {}

    for id_chunk in chunk_list(list(unique_rows.keys()), 200):
        result = (
            supabase.table("market_listings")
            .select("id,source_listing_id")
            .in_("source_listing_id", id_chunk)
            .execute()
        )

        if result.data:
            for row in result.data:
                listing_id_map[row["source_listing_id"]] = row["id"]

    print(f"Resolved {len(listing_id_map)} market_listing ids.")
    return listing_id_map


def upsert_listing_parses_batch(rows, chunk_size=100):
    if not rows:
        print("No listing_parses rows to upsert.")
        return

    print(f"Upserting {len(rows)} listing_parses rows...")

    row_chunks = list(chunk_list(rows, chunk_size))

    for chunk_index, row_chunk in enumerate(row_chunks, start=1):
        max_attempts = 3

        for attempt in range(1, max_attempts + 1):
            try:
                print(f"Upserting listing_parses chunk {chunk_index}/{len(row_chunks)} with {len(row_chunk)} rows (attempt {attempt})...")
                supabase.table("listing_parses").upsert(
                    row_chunk,
                    on_conflict="market_listing_id"
                ).execute()
                break
            except Exception as e:
                print(f"listing_parses chunk {chunk_index} failed on attempt {attempt}: {e}")
                if attempt == max_attempts:
                    raise
                time.sleep(2 ** attempt)

def build_listing_parse_rows(bundles, listing_id_map):
    rows = []

    for bundle in bundles:
        parsed = bundle["parsed"]
        market_listing_row = bundle["market_listing_row"]
        source_listing_id = market_listing_row.get("source_listing_id")
        market_listing_id = listing_id_map.get(source_listing_id)

        if not market_listing_id:
            continue

        matched_card = match_reference_card(parsed)

        row = {
            "market_listing_id": market_listing_id,
            "parse_version": "v1_parser",
            "pokemon_name": parsed.pokemon_name,
            "set_guess": parsed.set_guess,
            "card_number_guess": parsed.card_number_guess,
            "card_number_norm": parsed.card_number_norm,
            "promo_code_guess": parsed.promo_code_guess,
            "variant_guess": parsed.variant_guess,
            "language_guess": parsed.language_guess,
            "grade_company": parsed.grade_company,
            "grade_value": parsed.grade_value,
            "is_junk": parsed.is_junk,
            "junk_reason": parsed.junk_reason,
            "match_confidence": 0.97 if matched_card else None,
            "matched_card_id": matched_card["id"] if matched_card else None,
            "normalized_item_key": None,
            "parser_notes": {
                "normalized_title": parsed.normalized_title
            },
        }

        rows.append(row)

    return rows


def upsert_listing_parses_batch(rows, chunk_size=100):
    if not rows:
        print("No listing_parses rows to upsert.")
        return

    print(f"Upserting {len(rows)} listing_parses rows...")

    row_chunks = list(chunk_list(rows, chunk_size))

    for chunk_index, row_chunk in enumerate(row_chunks, start=1):
        max_attempts = 3

        for attempt in range(1, max_attempts + 1):
            try:
                print(f"Upserting listing_parses chunk {chunk_index}/{len(row_chunks)} with {len(row_chunk)} rows (attempt {attempt})...")
                supabase.table("listing_parses").upsert(
                    row_chunk,
                    on_conflict="market_listing_id"
                ).execute()
                break
            except Exception as e:
                print(f"listing_parses chunk {chunk_index} failed on attempt {attempt}: {e}")
                if attempt == max_attempts:
                    raise
                time.sleep(2 ** attempt)

def fetch_item_detail(access_token: str, item_id: str):
    url = f"https://api.ebay.com/buy/browse/v1/item/{item_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()

def fetch_item_detail_safe(access_token: str, item_id: str, max_retries: int = 4):
    for attempt in range(1, max_retries + 1):
        try:
            detail = fetch_item_detail(access_token, item_id)
            return {"item_id": item_id, "detail": detail, "error": None}
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status in (429, 500, 502, 503, 504) and attempt < max_retries:
                sleep_s = min(2 ** attempt, 20)
                time.sleep(sleep_s)
                continue
            return {"item_id": item_id, "detail": None, "error": f"http_{status}"}
        except Exception as e:
            if attempt < max_retries:
                time.sleep(min(2 ** attempt, 20))
                continue
            return {"item_id": item_id, "detail": None, "error": str(e)}


def enrich_items_with_details_concurrent(access_token: str, items, max_workers=10):
    enriched = []
    future_to_item = {}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for item in items:
            item_id = item.get("itemId")
            if not item_id:
                enriched.append((item, None))
                continue
            future = executor.submit(fetch_item_detail_safe, access_token, item_id)
            future_to_item[future] = item

        done_count = 0
        total = len(future_to_item)

        for future in as_completed(future_to_item):
            item = future_to_item[future]
            result = future.result()
            detail = result["detail"]
            error = result["error"]
            done_count += 1

            if error:
                print(f"[{done_count}/{total}] detail failed for {item.get('itemId')}: {error}")
            else:
                print(f"[{done_count}/{total}] detail ok for {item.get('itemId')}")

            enriched.append((item, detail))

    return enriched


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]



def inspect_item_details(access_token: str, item_ids):
    for item_id in item_ids:
        try:
            detail = fetch_item_detail(access_token, item_id)
            print("=" * 80)
            print("ITEM ID:", item_id)
            print("TITLE:", detail.get("title"))
            print("LOCALIZED ASPECTS:", detail.get("localizedAspects"))
            print("CATEGORY PATH:", detail.get("categoryPath"))
            print("DESCRIPTION:", detail.get("shortDescription"))
        except Exception as e:
            print(f"FAILED {item_id}: {e}")

def test_single_item(access_token: str, item_id: str):
    detail = fetch_item_detail(access_token, item_id)
    bundle = map_item_to_sold_comp(detail, detail)
    parsed = bundle["parsed"]
    print("TITLE:", detail.get("title"))
    print("set_guess:", parsed.set_guess)
    print("card_number_guess:", parsed.card_number_guess)
    print("card_number_norm:", parsed.card_number_norm)
    print("language_guess:", parsed.language_guess)

def run_charizard_enrichment_job(access_token: str, items, chunk_size=500, max_workers=10, start_index=0):
    target_items = items[start_index:]
    total = len(target_items)

    print(f"Starting enrichment job for {total} items from start_index={start_index}")

    for chunk_index, item_chunk in enumerate(chunked(target_items, chunk_size), start=1):
        print(f"Starting chunk {chunk_index} with {len(item_chunk)} items...")

        enriched_items = enrich_items_with_details_concurrent(
            access_token,
            item_chunk,
            max_workers=max_workers,
        )

        bundles = [map_item_to_sold_comp(item, detail) for item, detail in enriched_items]
        print(f"Built {len(bundles)} bundles for chunk {chunk_index}")

        market_rows = [b["market_listing_row"] for b in bundles]
        parse_rows = []
        sold_rows = [b["sold_comp_row"] for b in bundles]

        upsert_market_listings(market_rows)

        listing_ids = resolve_market_listing_ids([r["source_listing_id"] for r in market_rows])

        for b in bundles:
            parsed = b["parsed"]
            market_row = b["market_listing_row"]
            source_listing_id = market_row["source_listing_id"]
            listing_id = listing_ids.get(source_listing_id)

            if not listing_id:
                continue

            parse_rows.append(build_listing_parse_row(listing_id, parsed))

        upsert_listing_parses(parse_rows)
        insert_sold_comps(sold_rows)

        print(f"Finished chunk {chunk_index}")

def get_charizard_items(access_token: str):
    print("Fetching Charizard items from eBay...")

    try:
        items = fetch_charizard_items(access_token)
        print(f"Fetched {len(items)} live Charizard items from eBay.")
        return items
    except RuntimeError as e:
        print(f"Live eBay fetch failed: {e}")
        print("Falling back to stored Charizard items from database...")
        items = load_existing_charizard_items()
        print(f"Loaded {len(items)} stored Charizard items from database.")
        return items

def load_existing_charizard_items(limit=5000):
    rows = []
    page_size = 1000
    start = 0

    while True:
        response = (
            supabase.table("market_listings")
            .select("source_listing_id,raw_title,listing_url,raw_payload,created_at")
            .eq("source", "ebay")
            .ilike("raw_title", "%charizard%")
            .order("created_at", desc=True)
            .range(start, start + page_size - 1)
            .execute()
        )

        batch = response.data or []
        if not batch:
            break

        for row in batch:
            raw = row.get("raw_payload")

            if isinstance(raw, dict) and raw.get("itemId"):
                rows.append(raw)
            else:
                rows.append(
                    {
                        "itemId": row.get("source_listing_id"),
                        "title": row.get("raw_title"),
                        "itemWebUrl": row.get("listing_url"),
                    }
                )

        if len(rows) >= limit or len(batch) < page_size:
            break

        start += page_size

    return rows[:limit]

def get_charizard_items(access_token: str):
    print("Fetching Charizard items from eBay...")

    try:
        items = fetch_charizard_items(access_token)
        print(f"Fetched {len(items)} live Charizard items from eBay.")
        return items
    except RuntimeError as e:
        print(f"Live eBay fetch failed: {e}")
        print("Falling back to stored Charizard items from Supabase...")
        items = load_existing_charizard_items()
        print(f"Loaded {len(items)} stored Charizard items from Supabase.")
        return items


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--chunk-size", type=int, default=500)
    parser.add_argument("--max-workers", type=int, default=10)
    args = parser.parse_args()

    print("Getting eBay token...")
    token = get_ebay_access_token()

    items = get_charizard_items(token)

    run_charizard_enrichment_job(
        token,
        items,
        chunk_size=args.chunk_size,
        max_workers=args.max_workers,
        start_index=args.start_index,
    )

    print("Done.")


if __name__ == "__main__":
    main()
