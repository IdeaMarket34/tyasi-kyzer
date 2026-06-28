"""
reparse_listings.py  (v2 — fast batch mode)
============================================

Re-runs the current parser + matcher over every stored listing using only
raw_payload data already in market_listings. Makes NO eBay API calls.

WHAT'S NEW IN v2
----------------
v1 made ~5 Supabase round trips per listing:
  - get_or_create_set():    full pokemon_sets table scan on EVERY call
  - match_reference_card(): 1-3 card lookup queries per listing
  - upsert_parse():         1 write per listing
  - upsert_card_match():    1 write per listing

v2 fixes this with two changes:

  1. PRE-LOAD: all sets + all Charizard cards are loaded into memory at
     startup (a handful of queries total, regardless of listing count).
     get_or_create_set and match_reference_card are monkey-patched with
     in-memory versions that use these caches.

  2. BATCH WRITES: parse rows and card-match rows are accumulated and
     flushed in chunks of WRITE_BATCH_SIZE instead of one upsert per row.

Expected speedup: ~100-300x fewer DB calls.
48k listings: was several hours, now ~15-25 minutes.

USAGE
-----
  # Quick sanity check (no writes):
  python3 reparse_listings.py --dry-run --limit 500

  # Full dry-run to preview final match count:
  python3 reparse_listings.py --dry-run

  # Full run (writes listing_parses + listing_card_matches):
  python3 reparse_listings.py --apply
"""

import argparse
import importlib.util
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

from dotenv import load_dotenv, find_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
CWD = Path.cwd()

_loaded_from = None
for _cand in [find_dotenv(usecwd=True), str(SCRIPT_DIR / ".env"),
              find_dotenv(str(SCRIPT_DIR / ".env"))]:
    if _cand and Path(_cand).is_file():
        load_dotenv(_cand, override=False)
        _loaded_from = _cand
        break
print(f"Loaded environment from {_loaded_from}" if _loaded_from
      else "Note: no .env file found; relying on shell environment variables.")

REWRITE_PATH = SCRIPT_DIR / "charizard_ingest_rewrite.py"
if not REWRITE_PATH.is_file():
    REWRITE_PATH = CWD / "charizard_ingest_rewrite.py"


def load_rewrite():
    spec = importlib.util.spec_from_file_location("charizard_ingest_rewrite_runtime", str(REWRITE_PATH))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {REWRITE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rewrite            = load_rewrite()
supabase           = rewrite.supabase
map_item_to_bundle = rewrite.map_item_to_bundle

WRITE_BATCH_SIZE = 300   # rows per upsert call
READ_PAGE_SIZE   = 500   # listings per DB page fetch


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Pre-load: sets + cards into memory
# ---------------------------------------------------------------------------

def build_sets_cache() -> Tuple[Dict, Dict]:
    """
    Fetch all pokemon_sets in one query.
    Returns:
      sets_by_key   — keyed by set_key (exact match)
      sets_by_alias — keyed by normalized alias (strip non-alnum, lowercase)
                      covers set_key, set_name, and every aliases field entry
    """
    print("Pre-loading sets ...", end="", flush=True)
    result = (
        supabase.table("pokemon_sets")
        .select("id,set_key,set_name,aliases")
        .limit(5000)
        .execute()
    )
    sets_by_key:   Dict[str, dict] = {}
    sets_by_alias: Dict[str, dict] = {}

    def _norm(v: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (v or "").lower())

    for row in result.data or []:
        sets_by_key[row["set_key"]] = row
        sets_by_alias[_norm(row["set_key"])] = row
        if row.get("set_name"):
            sets_by_alias[_norm(row["set_name"])] = row
        for alias in (row.get("aliases") or "").split(","):
            alias = alias.strip()
            if alias:
                sets_by_alias[_norm(alias)] = row

    print(f" {len(sets_by_key)} sets loaded.")
    return sets_by_key, sets_by_alias


def build_cards_indexes() -> Tuple[Dict, Dict, Dict, Dict]:
    """
    Fetch all Charizard cards (paginated) and build four lookup indexes that
    mirror the four query paths in match_reference_card():

      promo_idx:     (promo_prefix, card_number, language)      -> [card]
      full_idx:      (set_id, card_number, total_in_set, lang)  -> [card]
      num_total_idx: (card_number, total_in_set, language)       -> [card]
      set_num_idx:   (set_id, card_number, language)             -> [card]
    """
    print("Pre-loading Charizard cards ...", end="", flush=True)
    all_cards: List[dict] = []
    page_size = 1000
    start = 0
    while True:
        result = (
            supabase.table("pokemon_cards")
            .select("id,card_key,pokemon_name,set_id,card_number_raw,card_number,"
                    "total_in_set,promo_prefix,language,metadata,pokemon_sets(set_key)")
            .eq("pokemon_name", "charizard")
            .range(start, start + page_size - 1)
            .execute()
        )
        batch = result.data or []
        all_cards.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size

    promo_idx:     Dict[tuple, List[dict]] = defaultdict(list)
    full_idx:      Dict[tuple, List[dict]] = defaultdict(list)
    num_total_idx: Dict[tuple, List[dict]] = defaultdict(list)
    set_num_idx:   Dict[tuple, List[dict]] = defaultdict(list)

    for card in all_cards:
        lang   = (card.get("language") or "en").lower()
        num    = str(card["card_number"])  if card.get("card_number")  else None
        total  = str(card["total_in_set"]) if card.get("total_in_set") else None
        set_id = card.get("set_id")
        prefix = card.get("promo_prefix")

        if prefix and num:
            promo_idx[(prefix.upper(), num, lang)].append(card)
        if set_id and num and total:
            full_idx[(set_id, num, total, lang)].append(card)
        if num and total:
            num_total_idx[(num, total, lang)].append(card)
        if set_id and num:
            set_num_idx[(set_id, num, lang)].append(card)

    print(f" {len(all_cards)} cards loaded.")
    return promo_idx, full_idx, num_total_idx, set_num_idx


# ---------------------------------------------------------------------------
# Replacement functions (monkey-patched onto the rewrite module)
# ---------------------------------------------------------------------------

def make_cached_get_or_create_set(sets_by_key, sets_by_alias, dry_run):
    """
    get_or_create_set() replacement that hits the in-memory cache first.
    Falls back to the original only on a true cache miss (a new auto-created
    set). In dry-run mode the fallback is skipped entirely.
    """
    _orig = rewrite.get_or_create_set

    def _norm(v: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (v or "").lower())

    def cached_fn(set_code=None, aspect_data=None):
        if not set_code and not (aspect_data or {}).get("set_name"):
            return None

        aspect_data = aspect_data or {}
        candidates  = [c for c in [set_code, aspect_data.get("set_name")] if c]

        for c in candidates:
            if c in sets_by_key:
                return sets_by_key[c]
            n = _norm(c)
            if n in sets_by_alias:
                return sets_by_alias[n]

        # True cache miss: fall back to original (rare during reparse)
        if not dry_run:
            result = _orig(set_code, aspect_data)
            if result and result.get("set_key"):
                sets_by_key[result["set_key"]] = result
                sets_by_alias[_norm(result["set_key"])] = result
            return result
        return None

    return cached_fn


def make_fast_match(sets_by_key, promo_idx, full_idx, num_total_idx, set_num_idx):
    """
    match_reference_card() replacement using only in-memory dict lookups.
    Exactly mirrors the four-path logic of the original.
    """
    def fast_match(parsed, aspects=None):
        if not parsed.pokemon_name or parsed.is_junk:
            return None

        aspects    = aspects or {}
        language   = (parsed.language or "en").lower()
        set_code   = parsed.set_code
        card_num   = parsed.card_number
        card_total = parsed.card_total_guess
        promo_code = parsed.promo_code

        # Supplement from eBay structured aspects (mirrors original)
        if not set_code and aspects.get("set_name"):
            set_code = (
                rewrite.detect_set_from_text(rewrite.normalize_text(aspects["set_name"]))
                or rewrite.normalize_set_key(aspects["set_name"])
            )
        if (not card_num or not card_total) and aspects.get("card_number_raw"):
            _, num, total, _ = rewrite.extract_fraction_fields(aspects["card_number_raw"])
            if num:   card_num   = num
            if total: card_total = total

        # Path 1: promo prefix + digits
        if promo_code:
            pm = re.match(r"^[a-z]+", promo_code)
            dm = re.search(r"(\d+)$", promo_code)
            if pm and dm:
                key  = (pm.group(0).upper(), str(int(dm.group(1))), language)
                hits = promo_idx.get(key, [])
                if len(hits) == 1:
                    return hits[0]

        # Path 2: set + number + total (tightest)
        if set_code and card_num and card_total:
            row = sets_by_key.get(set_code)
            if row:
                key  = (row["id"], str(card_num), str(card_total), language)
                hits = full_idx.get(key, [])
                if len(hits) == 1:
                    return hits[0]

        # Path 3: number + total only (set-agnostic, only if unambiguous)
        if card_num and card_total:
            key  = (str(card_num), str(card_total), language)
            hits = num_total_idx.get(key, [])
            if len(hits) == 1:
                return hits[0]

        # Path 4: set + number (no total)
        if set_code and card_num:
            row = sets_by_key.get(set_code)
            if row:
                key  = (row["id"], str(card_num), language)
                hits = set_num_idx.get(key, [])
                if len(hits) == 1:
                    return hits[0]

        return None

    return fast_match


# ---------------------------------------------------------------------------
# Batch write helpers
# ---------------------------------------------------------------------------

def flush_parse_rows(rows: List[dict]) -> None:
    if rows:
        supabase.table("listing_parses").upsert(rows, on_conflict="market_listing_id").execute()


def flush_card_match_rows(rows: List[dict]) -> None:
    if rows:
        supabase.table("listing_card_matches").upsert(rows, on_conflict="market_listing_id").execute()


# ---------------------------------------------------------------------------
# Listing page fetch
# ---------------------------------------------------------------------------

def fetch_listings_page(after_id: int, page_size: int) -> List[dict]:
    result = (
        supabase.table("market_listings")
        .select("id,source,source_listing_id,raw_payload,raw_title")
        .gt("id", after_id)
        .order("id", desc=False)
        .limit(page_size)
        .execute()
    )
    return result.data or []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Re-parse all stored listings (fast batch mode v2).")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="Preview only; don't write parses/matches (default).")
    mode.add_argument("--apply", action="store_true",
                      help="Write updated parses and matches to Supabase.")
    ap.add_argument("--limit",     type=int, default=None,
                    help="Process only the first N listings (for testing).")
    ap.add_argument("--page-size", type=int, default=READ_PAGE_SIZE,
                    help="DB page size for listing reads.")
    args = ap.parse_args()
    apply = args.apply
    if not apply:
        args.dry_run = True

    print(f"Mode: {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}")

    # ── Pre-load phase ────────────────────────────────────────────────────────
    sets_by_key, sets_by_alias = build_sets_cache()
    promo_idx, full_idx, num_total_idx, set_num_idx = build_cards_indexes()

    # ── Patch the ingest module's DB-heavy functions ──────────────────────────
    # map_item_to_bundle calls these by their unqualified names in the module's
    # global scope, so patching the module attributes redirects all internal calls.
    rewrite.get_or_create_set    = make_cached_get_or_create_set(
        sets_by_key, sets_by_alias, dry_run=not apply
    )
    rewrite.match_reference_card = make_fast_match(
        sets_by_key, promo_idx, full_idx, num_total_idx, set_num_idx
    )

    print("Caches ready. Starting reparse...\n")
    t_start = time.time()

    stats = {
        "processed": 0,
        "charizard": 0,
        "junk": 0,
        "matched": 0,
        "with_set": 0,
        "with_number_or_promo": 0,
        "errors": 0,
    }
    failures:             List[str]  = []
    pending_parses:       List[dict] = []
    pending_card_matches: List[dict] = []
    after_id = 0

    while True:
        rows = fetch_listings_page(after_id, args.page_size)
        if not rows:
            break

        for row in rows:
            after_id = row["id"]
            if args.limit and stats["processed"] >= args.limit:
                rows = []
                break

            payload = row.get("raw_payload") or {}
            if not payload:
                continue

            try:
                bundle = map_item_to_bundle(payload, payload)
                parse  = bundle["listing_parse_row"]
                stats["processed"] += 1

                if parse.get("pokemon_name") == "charizard": stats["charizard"] += 1
                if parse.get("is_junk"):                      stats["junk"]      += 1
                if parse.get("set_code"):                     stats["with_set"]  += 1
                if parse.get("card_number") or parse.get("promo_code"):
                    stats["with_number_or_promo"] += 1
                if parse.get("matched_card_id"):              stats["matched"]   += 1

                if apply:
                    parse_row = dict(parse)
                    parse_row["market_listing_id"] = row["id"]
                    notes = parse_row.get("parser_notes") or {}
                    if notes.get("resolved_set_id"):
                        parse_row["set_id"] = notes["resolved_set_id"]
                    pending_parses.append(parse_row)

                    if parse.get("matched_card_id"):
                        pending_card_matches.append({
                            "market_listing_id": row["id"],
                            "pokemon_card_id":   parse["matched_card_id"],
                            "match_method":      "reparse_listings",
                            "match_confidence":  parse.get("match_confidence"),
                            "evidence_json": {
                                "normalized_item_key": parse.get("normalized_item_key"),
                                "set_code":    parse.get("set_code"),
                                "card_number": parse.get("card_number"),
                                "promo_code":  parse.get("promo_code"),
                            },
                            "updated_at": utc_now(),
                        })

                    if len(pending_parses) >= WRITE_BATCH_SIZE:
                        flush_parse_rows(pending_parses)
                        flush_card_match_rows(pending_card_matches)
                        pending_parses       = []
                        pending_card_matches = []

            except Exception as e:
                stats["errors"] += 1
                if len(failures) < 10:
                    failures.append(f"id={row['id']} ({(row.get('raw_title') or '')[:40]}): {e}")

            if stats["processed"] % 1000 == 0 and stats["processed"]:
                elapsed = time.time() - t_start
                rate    = stats["processed"] / elapsed if elapsed else 0
                print(f"  ...{stats['processed']:,} processed  "
                      f"({rate:.0f}/s)  matched so far: {stats['matched']:,}")

        if args.limit and stats["processed"] >= args.limit:
            break
        if not rows:
            break

    # Final flush
    if apply:
        flush_parse_rows(pending_parses)
        flush_card_match_rows(pending_card_matches)

    elapsed = time.time() - t_start
    print(f"\n================ RE-PARSE SUMMARY ================")
    print(f"Listings processed:           {stats['processed']:,}")
    print(f"  identified as Charizard:    {stats['charizard']:,}")
    print(f"  flagged as junk/non-single: {stats['junk']:,}")
    print(f"  with a set detected:        {stats['with_set']:,}")
    print(f"  with a number or promo:     {stats['with_number_or_promo']:,}")
    print(f"  >>> MATCHED to a card:      {stats['matched']:,}")
    print(f"  errors:                     {stats['errors']:,}")
    print(f"Time elapsed:                 {elapsed / 60:.1f} min")
    if failures:
        print(f"\nFirst few errors:")
        for f in failures:
            print("  ", f)
    if not apply:
        print(f"\nDRY-RUN: nothing written. Re-run with --apply when ready.")
    print("===================================================")


if __name__ == "__main__":
    main()