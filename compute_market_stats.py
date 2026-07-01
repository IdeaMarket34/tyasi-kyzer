"""
compute_market_stats.py
========================

Computes a fair "market price" per canonical item (normalized_item_key) from
your sold comps + active asking listings, per the pricing spec
(charizard_dealfinder_pricing_spec.md), and writes the result into market_stats.

WHAT IT DOES, PER GROUP
-----------------------
  1. Pull sold comps (is_valid_comp = true) and active matched listings.
  2. Outlier-filter each side independently (modified z-score in log space for
     n>=5, a blunt fixed band for 2<=n<5, no filtering for n<2).
  3. Weight sold comps by recency (60-day half-life, 90-day hard cutoff).
  4. Combine into one weighted median, with sold comps holding a fixed 75%
     share of the vote when both sides have data (not a per-point multiplier
     -- see the spec for why that distinction matters).
  5. Cold-start fallback (asking-derived estimate) when there are no usable
     sold comps, using a discount factor calibrated from your own data.
  6. Assign a confidence tier (HIGH/MEDIUM/LOW) so the UI can show the price
     with the right amount of trust.

SCOPE: only items with a real matched_card_id get a price. Averaging across
the unresolved/junk catch-all buckets would produce a confident-looking number
for a pile of unrelated items -- worse than showing nothing.

SAFETY: --dry-run (default) prints what WOULD be written, no DB writes.
Requires extend_market_stats.sql to have been run first (adds the columns
this script writes, and a unique index for the upsert).

USAGE
-----
  python3 compute_market_stats.py --dry-run
  python3 compute_market_stats.py --dry-run --limit 20      # quick sanity check
  python3 compute_market_stats.py --apply
"""

import argparse
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv, find_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
_loaded_from = None
for _cand in [find_dotenv(usecwd=True), str(SCRIPT_DIR / ".env"),
              find_dotenv(str(SCRIPT_DIR / ".env"))]:
    if _cand and Path(_cand).is_file():
        load_dotenv(_cand, override=False)
        _loaded_from = _cand
        break
print(f"Loaded environment from {_loaded_from}" if _loaded_from
      else "Note: no .env file found; relying on shell environment variables.")

import os
from supabase import create_client

# ---- spec defaults (charizard_dealfinder_pricing_spec.md, section 7) -------
OUTLIER_Z_THRESHOLD = 3.5
OUTLIER_BAND_LOW, OUTLIER_BAND_HIGH = 0.25, 4.0
LOOKBACK_DAYS = 90
HALF_LIFE_DAYS = 60
TARGET_SOLD_SHARE = 0.75
DEFAULT_COLD_START_DISCOUNT = 0.85  # placeholder; recalibrated below from real data


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def days_ago(iso_ts: str) -> float:
    if not iso_ts:
        return 9999
    ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0


# ---- section 1: outlier filtering ------------------------------------------

def filter_outliers(prices: List[float]) -> Tuple[List[float], int]:
    """Returns (kept_prices, n_dropped). Operates on a plain price list --
    caller is responsible for keeping any associated weights/timestamps
    aligned (we return an index mask instead of touching prices in place)."""
    n = len(prices)
    if n < 2:
        return prices, 0

    if n < 5:
        med = statistics.median(prices)
        low, high = med * OUTLIER_BAND_LOW, med * OUTLIER_BAND_HIGH
        kept = [p for p in prices if low <= p <= high]
        return kept, n - len(kept)

    logs = [math.log(p) for p in prices]
    med = statistics.median(logs)
    mad = statistics.median([abs(x - med) for x in logs])
    if mad == 0:
        return prices, 0
    kept = []
    for p, x in zip(prices, logs):
        z = 0.6745 * (x - med) / mad
        if abs(z) <= OUTLIER_Z_THRESHOLD:
            kept.append(p)
    return kept, n - len(kept)


def filter_outliers_with_meta(items: List[dict], price_key: str) -> Tuple[List[dict], int]:
    """Same filtering logic, but keeps each item's other fields (e.g. sold_at)
    attached to its price instead of just returning a bare price list."""
    prices = [it[price_key] for it in items]
    n = len(prices)
    if n < 2:
        return items, 0
    if n < 5:
        med = statistics.median(prices)
        low, high = med * OUTLIER_BAND_LOW, med * OUTLIER_BAND_HIGH
        kept = [it for it in items if low <= it[price_key] <= high]
        return kept, n - len(kept)
    logs = [math.log(it[price_key]) for it in items]
    med = statistics.median(logs)
    mad = statistics.median([abs(x - med) for x in logs])
    if mad == 0:
        return items, 0
    kept = []
    for it, x in zip(items, logs):
        z = 0.6745 * (x - med) / mad
        if abs(z) <= OUTLIER_Z_THRESHOLD:
            kept.append(it)
    return kept, n - len(kept)


# ---- section 3: weighted median with target-share normalization -----------

def weighted_median(price_weight_pairs: List[Tuple[float, float]]) -> Optional[float]:
    if not price_weight_pairs:
        return None
    pairs = sorted(price_weight_pairs, key=lambda pw: pw[0])
    total = sum(w for _, w in pairs)
    if total <= 0:
        return None
    cum = 0.0
    for price, w in pairs:
        cum += w
        if cum >= total / 2:
            return price
    return pairs[-1][0]


def compute_combined_price(
    sold_filtered: List[dict],   # [{"price": ..., "sold_at": iso_str}]
    ask_filtered: List[float],
) -> Tuple[Optional[float], dict]:
    """Implements spec section 3: recency-weighted sold comps, normalized to a
    fixed target group share, combined with asking listings into one weighted
    median. Returns (market_price, debug_info)."""
    sold_recency_weights = []
    for s in sold_filtered:
        age = days_ago(s["sold_at"])
        w = math.exp(-math.log(2) * age / HALF_LIFE_DAYS)
        sold_recency_weights.append(w)

    raw_sold_weight_sum = sum(sold_recency_weights)
    raw_ask_weight_sum = float(len(ask_filtered))

    pairs: List[Tuple[float, float]] = []

    if raw_sold_weight_sum > 0 and raw_ask_weight_sum > 0:
        scale_sold = TARGET_SOLD_SHARE / raw_sold_weight_sum
        scale_ask = (1 - TARGET_SOLD_SHARE) / raw_ask_weight_sum
        for s, w in zip(sold_filtered, sold_recency_weights):
            pairs.append((s["price"], w * scale_sold))
        for p in ask_filtered:
            pairs.append((p, scale_ask))
    elif raw_sold_weight_sum > 0:
        for s, w in zip(sold_filtered, sold_recency_weights):
            pairs.append((s["price"], w))
    # else: no sold comps at all -- handled by the cold-start path in the caller

    price = weighted_median(pairs) if pairs else None
    return price, {"raw_sold_weight_sum": raw_sold_weight_sum, "raw_ask_weight_sum": raw_ask_weight_sum}


# ---- main per-group computation --------------------------------------------

def compute_group_stats(normalized_item_key: str, sold_rows: List[dict],
                         ask_prices: List[float], cold_start_discount: float) -> dict:
    n_sold_raw = len(sold_rows)
    n_ask_raw = len(ask_prices)

    sold_filtered, sold_dropped = filter_outliers_with_meta(sold_rows, "price")
    n_sold_filtered = len(sold_filtered)

    ask_filtered, ask_dropped = filter_outliers(ask_prices)
    n_ask_filtered = len(ask_filtered)

    sold_median = statistics.median([s["price"] for s in sold_filtered]) if sold_filtered else None
    ask_median = statistics.median(ask_filtered) if ask_filtered else None

    if n_sold_filtered == 0:
        # cold start: no usable sold comps, fall back to a discounted asking median
        market_price = (ask_median * cold_start_discount) if ask_median is not None else None
        confidence = "LOW"
    else:
        market_price, _debug = compute_combined_price(sold_filtered, ask_filtered)
        confidence = "HIGH" if n_sold_filtered >= 5 else "MEDIUM"

    combined_for_minmax = [s["price"] for s in sold_filtered] + ask_filtered
    return {
        "normalized_item_key": normalized_item_key,
        "market_price": round(market_price, 2) if market_price is not None else None,
        "sold_median": round(sold_median, 2) if sold_median is not None else None,
        "ask_median": round(ask_median, 2) if ask_median is not None else None,
        "n_sold_raw": n_sold_raw,
        "n_sold_filtered": n_sold_filtered,
        "n_ask_raw": n_ask_raw,
        "n_ask_filtered": n_ask_filtered,
        "confidence": confidence,
        # legacy columns the table already had, kept populated for anything
        # else already reading them
        "median_price": round(market_price, 2) if market_price is not None else None,
        "sample_size": n_sold_filtered + n_ask_filtered,
        "min_price": round(min(combined_for_minmax), 2) if combined_for_minmax else None,
        "max_price": round(max(combined_for_minmax), 2) if combined_for_minmax else None,
        "avg_price": round(sum(combined_for_minmax) / len(combined_for_minmax), 2) if combined_for_minmax else None,
    }


def calibrate_cold_start_discount(all_group_data: Dict[str, dict]) -> Tuple[float, list]:
    """Spec section 3: ASK_TO_SOLD_DISCOUNT should be calibrated from groups
    that have BOTH enough sold comps and enough asking listings to compare.
    Falls back to the spec's placeholder if there isn't enough data yet.
    Returns (discount, ratio_details) so the caller can inspect what the
    number is actually based on before trusting it."""
    ratio_details = []
    for key, g in all_group_data.items():
        if g["n_sold_filtered"] >= 3 and g["n_ask_filtered"] >= 3 and g["sold_median"] and g["ask_median"]:
            ratio_details.append({
                "key": key, "ratio": g["sold_median"] / g["ask_median"],
                "sold_median": g["sold_median"], "ask_median": g["ask_median"],
                "n_sold": g["n_sold_filtered"], "n_ask": g["n_ask_filtered"],
            })
    if len(ratio_details) >= 5:
        discount = round(statistics.median([r["ratio"] for r in ratio_details]), 4)
        return discount, ratio_details
    return DEFAULT_COLD_START_DISCOUNT, ratio_details


def main() -> None:
    ap = argparse.ArgumentParser(description="Compute per-group market price stats.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Preview only (default).")
    mode.add_argument("--apply", action="store_true", help="Write to market_stats.")
    ap.add_argument("--limit", type=int, default=None, help="Only process the first N groups (testing).")
    args = ap.parse_args()
    apply = args.apply
    if not apply:
        args.dry_run = True

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("ERROR: needs SUPABASE_URL and SUPABASE_SERVICE_KEY in your environment.")
        return
    supabase = create_client(url, key)

    print("Pulling valid sold comps...")
    sold_by_key: Dict[str, List[dict]] = {}
    offset = 0
    page_size = 500
    while True:
        page = (
            supabase.table("sold_comps")
            .select("id,normalized_item_key,total_price,sold_at")
            .eq("is_valid_comp", True)
            .order("id")
            .range(offset, offset + page_size - 1)
            .execute()
        ).data or []
        if not page:
            break
        for row in page:
            age = days_ago(row["sold_at"])
            if age > LOOKBACK_DAYS:
                continue  # spec section 2: hard cutoff, applied before filtering
            key = row["normalized_item_key"]
            sold_by_key.setdefault(key, []).append({"price": float(row["total_price"]), "sold_at": row["sold_at"]})
        offset += page_size
    print(f"  {sum(len(v) for v in sold_by_key.values())} valid, in-window sold comps "
          f"across {len(sold_by_key)} groups")

    print("Pulling active, matched listings (asking side)...")
    ask_by_key: Dict[str, List[float]] = {}
    after_id = 0
    while True:
        page = (
            supabase.table("listing_parses")
            .select("market_listing_id,normalized_item_key,matched_card_id,is_junk,"
                     "market_listings(price_value,current_price_value,shipping_value,listing_status)")
            .not_.is_("matched_card_id", "null")
            .gt("market_listing_id", after_id)
            .order("market_listing_id")
            .limit(500)
            .execute()
        ).data or []
        if not page:
            break
        for row in page:
            after_id = row["market_listing_id"]
            ml = row.get("market_listings") or {}
            if ml.get("listing_status") != "active":
                continue
            if row.get("is_junk"):
                continue
            price = ml.get("current_price_value")
            if price is None:
                price = ml.get("price_value")
            if price is None:
                continue
            shipping = ml.get("shipping_value") or 0.0  # missing shipping treated as $0 (likely free shipping)
            total = float(price) + float(shipping)
            ask_by_key.setdefault(row["normalized_item_key"], []).append(total)
    print(f"  {sum(len(v) for v in ask_by_key.values())} active matched listings "
          f"across {len(ask_by_key)} groups")

    all_keys = sorted(set(sold_by_key) | set(ask_by_key))
    if args.limit:
        all_keys = all_keys[:args.limit]
    print(f"\nComputing stats for {len(all_keys)} groups...")

    # First pass with the placeholder discount, so we have sold/ask medians
    # available to calibrate a real discount factor from this run's own data.
    prelim = {
        k: compute_group_stats(k, sold_by_key.get(k, []), ask_by_key.get(k, []), DEFAULT_COLD_START_DISCOUNT)
        for k in all_keys
    }
    discount, ratio_details = calibrate_cold_start_discount(prelim)
    n_calibration_groups = len(ratio_details)
    calibrated = n_calibration_groups >= 5
    print(f"Cold-start discount factor: {discount} "
          f"({'calibrated from ' + str(n_calibration_groups) + ' groups' if calibrated else 'using spec placeholder -- not enough data yet to calibrate'})")
    if ratio_details:
        ratios_only = [r["ratio"] for r in ratio_details]
        print("  Groups behind this number (sold_median / ask_median for each):")
        for r in sorted(ratio_details, key=lambda r: r["ratio"]):
            print(f"    {r['key']:35} ratio={r['ratio']:.3f}  "
                  f"sold=${r['sold_median']} (n={r['n_sold']})  ask=${r['ask_median']} (n={r['n_ask']})")
        if calibrated:
            spread = max(ratios_only) / min(ratios_only)
            print(f"  Spread (max/min ratio): {spread:.2f}x")
            if spread > 3:
                print("  WARNING: wide spread across a small sample -- this calibrated number")
                print("  may not be reliable yet. Consider keeping the 0.85 placeholder for")
                print("  another cycle until more groups qualify, rather than trusting this.")

    # Re-run any cold-start groups with the (possibly newly calibrated) discount.
    results = []
    for k in all_keys:
        g = prelim[k]
        if g["n_sold_filtered"] == 0 and g["ask_median"] is not None:
            g = compute_group_stats(k, sold_by_key.get(k, []), ask_by_key.get(k, []), discount)
        results.append(g)

    by_confidence = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for g in results:
        by_confidence[g["confidence"]] += 1

    print(f"\n================ SUMMARY ================")
    print(f"Groups computed: {len(results)}")
    print(f"  HIGH confidence:   {by_confidence['HIGH']}")
    print(f"  MEDIUM confidence: {by_confidence['MEDIUM']}")
    print(f"  LOW confidence:    {by_confidence['LOW']}")
    print("\nSample results:")
    for g in results[:8]:
        print(f"  {g['normalized_item_key']:35} market=${g['market_price']:<8} "
              f"sold_med=${g['sold_median']} (n={g['n_sold_filtered']})  "
              f"ask_med=${g['ask_median']} (n={g['n_ask_filtered']})  conf={g['confidence']}")
    print("===========================================")

    if not apply:
        print("\nDRY-RUN: nothing written to market_stats. Re-run with --apply to write.")
        return

    print(f"\nWriting {len(results)} rows to market_stats...")
    # Explicit allow-list of real market_stats columns (7 original + 7 added by
    # extend_market_stats.sql). compute_group_stats' dict also carries
    # "market_price" purely for console display -- it is NOT a real column
    # (the column holding that value is "median_price"), so it's deliberately
    # excluded here rather than sent and rejected by the DB.
    MARKET_STATS_COLUMNS = {
        "normalized_item_key", "median_price", "sample_size", "min_price",
        "max_price", "avg_price", "last_refreshed_at",
        "sold_median", "ask_median", "n_sold_raw", "n_sold_filtered",
        "n_ask_raw", "n_ask_filtered", "confidence",
    }
    chunk = 200
    for i in range(0, len(results), chunk):
        batch = results[i:i + chunk]
        upload_batch = []
        for row in batch:
            row["last_refreshed_at"] = utc_now()
            upload_batch.append({k: v for k, v in row.items() if k in MARKET_STATS_COLUMNS})
        supabase.table("market_stats").upsert(upload_batch, on_conflict="normalized_item_key").execute()
    print("Done.")


if __name__ == "__main__":
    main()