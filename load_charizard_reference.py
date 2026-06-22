import os
from datetime import datetime, timezone
from typing import Optional

import requests
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

POKEMON_TCG_API_URL = "https://api.pokemontcg.io/v2/cards"


def normalize_set_key(name: str, ptcgo_code: Optional[str] = None) -> str:
    if ptcgo_code:
        return ptcgo_code.lower().replace("-", "_")
    return (
        name.lower()
        .replace("&", "and")
        .replace("'", "")
        .replace(":", "")
        .replace("-", " ")
        .replace("/", " ")
        .strip()
        .replace(" ", "_")
    )


def normalize_card_number(number: str, printed_total=None) -> str:
    raw_number = str(number).strip().lower().replace(" ", "")
    raw_number = raw_number.replace("-", "_")

    if printed_total:
        raw_total = str(printed_total).strip().lower().replace(" ", "")
        if "/" not in raw_number and "_" not in raw_number:
            return f"{raw_number}_{raw_total}"

    return raw_number.replace("/", "_")

def fetch_charizard_cards():
    all_cards = []
    page = 1
    page_size = 250

    while True:
        params = {
            "q": 'name:"Charizard"',
            "page": page,
            "pageSize": page_size,
        }

        response = requests.get(POKEMON_TCG_API_URL, params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()

        cards = payload.get("data", [])
        if not cards:
            break

        all_cards.extend(cards)

        total_count = payload.get("totalCount", 0)
        if len(all_cards) >= total_count:
            break

        page += 1

    return all_cards


def upsert_set(card_set: dict) -> int:
    set_key = normalize_set_key(card_set.get("name", ""), card_set.get("ptcgoCode"))

    row = {
        "set_key": set_key,
        "set_name": card_set.get("name"),
        "series_name": card_set.get("series"),
        "set_code": card_set.get("ptcgoCode"),
        "language": "en",
        "release_date": card_set.get("releaseDate"),
        "aliases": [x for x in [card_set.get("name"), card_set.get("ptcgoCode")] if x],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    result = (
        supabase.table("pokemon_sets")
        .upsert(row, on_conflict="set_key")
        .execute()
    )

    return result.data[0]["id"]


def upsert_card(card: dict, set_id: int, set_key: str):
    number = card.get("number")
    printed_total = card.get("set", {}).get("printedTotal")
    number_norm = normalize_card_number(number, printed_total)

    rarity = card.get("rarity")
    supertype = card.get("supertype")

    subtypes = card.get("subtypes") or []
    subtype = ",".join(subtypes) if subtypes else None

    row = {
        "card_key": f'charizard|{set_key}|{number_norm}|en',
        "pokemon_name": "charizard",
        "set_id": set_id,
        "card_number": number,
        "card_number_norm": number_norm,
        "total_in_set": str(card.get("set", {}).get("printedTotal")) if card.get("set", {}).get("printedTotal") else None,
        "promo_prefix": None,
        "rarity": rarity,
        "supertype": supertype,
        "subtype": subtype,
        "variant_family": None,
        "language": "en",
        "image_url": (card.get("images") or {}).get("small"),
        "metadata": {
            "api_card_id": card.get("id"),
            "tcgplayer_url": card.get("tcgplayer", {}).get("url") if card.get("tcgplayer") else None,
            "set_name": card.get("set", {}).get("name"),
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    return (
        supabase.table("pokemon_cards")
        .upsert(row, on_conflict="card_key")
        .execute()
    )


def main():
    print("Fetching Charizard cards from Pokemon TCG API...")
    cards = fetch_charizard_cards()
    print(f"Fetched {len(cards)} cards")

    inserted = 0

    for card in cards:
        card_set = card.get("set") or {}
        set_key = normalize_set_key(card_set.get("name", ""), card_set.get("ptcgoCode"))
        set_id = upsert_set(card_set)
        upsert_card(card, set_id, set_key)
        inserted += 1

    print(f"Upserted {inserted} Charizard reference cards")


if __name__ == "__main__":
    main()
