#!/usr/bin/env python3
"""
image_analysis_worker.py — Charizard DealFinder

Downloads listing primary images, computes pHash for near-duplicate detection,
and runs Tesseract OCR to extract card numbers from the card face.

Results are written to listing_parses.image_analysis (jsonb).
Junk listings are flagged via is_junk=true / junk_reason.

Junk signals:
  - download_failed      : image URL is dead
  - placeholder_image    : image is < 80px in any dimension
  - landscape_bulk_photo : width/height > 1.3 (bulk lots, scene photos)

OCR output:
  - ocr_card_number      : "XXX/YYY" extracted from bottom strip of card
  - card_number_match    : "confirm" | "conflict" | "not_found"
                           compared against listing_parses.card_number

Triggered by pg_cron → trigger_github_workflow('image-analysis.yml').
"""

import io
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import imagehash
import pytesseract
import requests
from PIL import Image, ImageOps
from supabase import create_client, Client

# ── Config ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
BATCH_SIZE = int(os.environ.get("IMAGE_BATCH_SIZE", "150"))
IMAGE_TIMEOUT = int(os.environ.get("IMAGE_TIMEOUT_SECONDS", "10"))

# Junk thresholds
MIN_DIMENSION_PX = 80    # smaller than this → placeholder/broken image
LANDSCAPE_RATIO = 1.3    # w/h > this → landscape → likely bulk lot or scene photo

# pHash matching thresholds (hash_size=8 → 64-bit hash, max hamming distance 64).
# Conservative starting point per image_recognition_buildvsbuy.md research —
# tune empirically once we have real distance distributions from session #30's
# first production run. Two tiers: auto-accept (confident enough to write
# matched_card_id directly) vs. candidate (queued for manual review).
PHASH_AUTO_ACCEPT_MAX_DISTANCE = 10
# Tightened 18 -> 14 in the session following #31's card_number cross-check
# rollout: production data showed distance 16-18 conflicting with the
# parser's card_number 94-100% of the time (3/3 at distance 16, 15/16 at
# distance 18), confirming that band is mostly noise rather than real
# matches. No card_number-agreement data exists yet for distance 12-14
# specifically (no candidates landed there in this run), so this cut is
# evidence-backed only down to 14 — not a further blind guess. Revisit again
# once more volume lands in the 12-14 band.
PHASH_CANDIDATE_MAX_DISTANCE = 14

# OCR: card numbers appear in bottom strip as "025/198", "TG01/TG30", etc.
CARD_NUMBER_RE = re.compile(r'\b(\d{1,3})\s*/\s*(\d{1,3}[A-Z]*)\b')

# Keywords that appear on real card faces (Tesseract doesn't need to be perfect)
POKEMON_KEYWORDS = {
    "charizard", "pokémon", "pokemon", "hp", "weakness",
    "resistance", "retreat", "stage", "evolves",
}

# ── Supabase ──────────────────────────────────────────────────────────────────

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def fetch_batch() -> list[dict]:
    """Fetch rows needing image analysis via the DB RPC."""
    resp = supabase.rpc("fetch_image_analysis_batch", {"p_limit": BATCH_SIZE}).execute()
    return resp.data or []


def fetch_reference_phashes() -> list[dict]:
    """
    Load the full reference phash library once per run.

    Only ~96 rows (session #29 Charizard library), so loading the whole table
    into memory and comparing in Python is cheap — no need for per-row queries
    or DB-side hamming distance. This table holds hashes + provenance only;
    the actual reference images stay local on the user's Mac and are never
    read by this worker (it runs in GitHub Actions, with no access to that
    filesystem — see reference_phashes table comment / sync_reference_phashes.py).
    """
    resp = supabase.table("reference_phashes").select(
        "pokemon_card_id, card_key, phash, pokemon_cards(card_number)"
    ).execute()
    rows = resp.data or []
    # Flatten the joined pokemon_cards.card_number onto each row so downstream
    # code (find_best_phash_match) can treat this like any other flat field.
    for row in rows:
        joined = row.pop("pokemon_cards", None) or {}
        row["card_number"] = joined.get("card_number")
    return rows


# ── Image download ────────────────────────────────────────────────────────────

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; CharizardDealFinder/1.0)"}


def download_image(url: str) -> Image.Image | None:
    """Download and return a PIL Image in RGB mode, or None on failure."""
    try:
        r = requests.get(url, timeout=IMAGE_TIMEOUT, headers=HEADERS)
        r.raise_for_status()
        return Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception as e:
        log.debug(f"Download failed [{url}]: {e}")
        return None


# ── pHash ─────────────────────────────────────────────────────────────────────

def compute_phash(img: Image.Image) -> str:
    """Return an 8-bit perceptual hash hex string."""
    return str(imagehash.phash(img, hash_size=8))


def find_best_phash_match(phash_hex: str, reference_library: list[dict]) -> dict | None:
    """
    Compare a listing's phash against every reference phash in memory and
    return the closest match plus its hamming distance, or None if the
    reference library is empty.

    reference_library entries: {pokemon_card_id, card_key, phash}.
    """
    if not reference_library:
        return None

    query_hash = imagehash.hex_to_hash(phash_hex)
    best = None
    best_distance = None

    for ref in reference_library:
        ref_hash = imagehash.hex_to_hash(ref["phash"])
        distance = int(query_hash - ref_hash)  # imagehash overloads `-` as hamming distance; cast numpy.int64 -> int for JSON serialization
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best = ref

    return {
        "pokemon_card_id": best["pokemon_card_id"],
        "card_key": best["card_key"],
        "card_number": best.get("card_number"),
        "distance": best_distance,
    }


def phash_match_tier(distance: int) -> str:
    """Classify a hamming distance into 'auto_accept', 'candidate', or 'no_match'."""
    if distance <= PHASH_AUTO_ACCEPT_MAX_DISTANCE:
        return "auto_accept"
    if distance <= PHASH_CANDIDATE_MAX_DISTANCE:
        return "candidate"
    return "no_match"


# ── OCR ───────────────────────────────────────────────────────────────────────

def ocr_bottom_strip(img: Image.Image) -> str:
    """
    OCR the bottom 20% of the image where the card number lives.
    Upscales 3× and restricts character set for better accuracy.
    """
    w, h = img.size
    strip = img.crop((0, int(h * 0.80), w, h))
    gray = ImageOps.grayscale(strip)
    gray = gray.resize((gray.width * 3, gray.height * 3), Image.LANCZOS)
    return pytesseract.image_to_string(
        gray,
        config=(
            "--psm 6 "
            "-c tessedit_char_whitelist="
            "0123456789/ABCDEFGHIJKLMNOPQRSTUVWXYZTH "
        ),
    )


def ocr_full_image(img: Image.Image) -> str:
    """
    Sparse OCR over the full image to detect any Pokémon card keywords.
    Used to confirm the image looks like a card, not as a precise data source.
    """
    gray = ImageOps.grayscale(img)
    return pytesseract.image_to_string(gray, config="--psm 11")


def extract_card_number(text: str) -> str | None:
    """Return the first 'XXX/YYY' match in the text, or None."""
    m = CARD_NUMBER_RE.search(text)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def has_pokemon_content(text: str) -> bool:
    """True if any Pokémon card keyword appears in the OCR text."""
    lower = text.lower()
    return any(kw in lower for kw in POKEMON_KEYWORDS)


# ── Junk detection ────────────────────────────────────────────────────────────

def detect_junk_signals(w: int, h: int) -> list[str]:
    """
    Return a list of junk signal strings based on image dimensions.
    Empty list = not junk.

    Intentionally conservative: we only auto-junk on clear structural signals
    (dead link, placeholder size, landscape orientation). Portrait images where
    OCR simply fails are logged but not marked junk — false positives on real
    listings are worse than missing some junk.
    """
    signals = []
    if w < MIN_DIMENSION_PX or h < MIN_DIMENSION_PX:
        signals.append("placeholder_image")
    if w / h > LANDSCAPE_RATIO:
        signals.append("landscape_bulk_photo")
    return signals


# ── Card number comparison ────────────────────────────────────────────────────

def normalize_card_number(raw: str | None) -> str | None:
    """
    Strip leading zeros for apples-to-apples comparison.
    Handles both OCR format ("025/198" → "25") and parser format ("025" → "25").
    Returns None for non-numeric identifiers like "TG01" or "SWSH001".
    """
    if not raw:
        return None
    # Extract the leading numeric portion — works for "025/198", "025", "25"
    m = re.match(r'^(\d+)', raw.strip())
    if not m:
        return None
    try:
        return str(int(m.group(1)))
    except ValueError:
        return None


def card_number_verdict(ocr_num: str | None, parse_num: str | None) -> str:
    """
    Compare OCR-extracted card number to the parser's card_number.
    Returns 'confirm', 'conflict', or 'not_found'.
    """
    if not ocr_num or not parse_num:
        return "not_found"
    return "confirm" if normalize_card_number(ocr_num) == normalize_card_number(parse_num) else "conflict"


def phash_card_number_verdict(ref_card_num: str | None, parse_num: str | None) -> str:
    """
    Compare a phash match's reference card_number to the parser's card_number.
    Returns 'confirm', 'conflict', or 'not_found'.

    This is the session #31 reprint/identical-artwork mitigation: phash alone
    cannot distinguish cards with identical artwork printed across different
    sets/numbers (e.g. daa|20 vs shf|SV107). A two-signal agreement (phash +
    parser both landing on the same card_number) is much stronger evidence
    than phash distance alone — and a disagreement is a strong signal the
    phash match is wrong, even at a low hamming distance.
    """
    if not ref_card_num or not parse_num:
        return "not_found"
    return "confirm" if normalize_card_number(ref_card_num) == normalize_card_number(parse_num) else "conflict"


# ── Row processing ────────────────────────────────────────────────────────────

def process_row(row: dict, reference_library: list[dict]) -> dict:
    """
    Download and analyze one listing's primary image.
    Returns the full image_analysis payload to be stored as jsonb.
    """
    url = row["primary_image_url"]
    analysis: dict = {
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "image_url": url,
    }

    img = download_image(url)
    if img is None:
        analysis["download_success"] = False
        analysis["junk_signals"] = ["download_failed"]
        analysis["junk_detected"] = True
        return analysis

    w, h = img.size
    phash_hex = compute_phash(img)
    analysis.update({
        "download_success": True,
        "image_width": w,
        "image_height": h,
        "aspect_ratio": round(w / h, 4),
        "phash": phash_hex,
    })

    # OCR: bottom strip first (most reliable for card numbers), then full image
    strip_text = ocr_bottom_strip(img)
    full_text = ocr_full_image(img)
    card_num_ocr = extract_card_number(strip_text) or extract_card_number(full_text)
    pokemon_content = has_pokemon_content(full_text)

    analysis.update({
        "ocr_card_number": card_num_ocr,
        "ocr_has_pokemon_content": pokemon_content,
        "ocr_full_text": full_text[:600],  # truncate for storage
        "card_number_match": card_number_verdict(card_num_ocr, row.get("card_number")),
    })

    signals = detect_junk_signals(w, h)
    analysis["junk_signals"] = signals
    analysis["junk_detected"] = bool(signals)

    # pHash matching against the session #29 reference library. Skipped
    # entirely if the image was already flagged junk (bulk lots / placeholders
    # are not worth comparing) or if the library hasn't been synced yet.
    if not analysis["junk_detected"]:
        match = find_best_phash_match(phash_hex, reference_library)
        if match is not None:
            tier = phash_match_tier(match["distance"])
            agreement = phash_card_number_verdict(match.get("card_number"), row.get("card_number"))
            analysis["phash_match"] = {
                "pokemon_card_id": match["pokemon_card_id"],
                "card_key": match["card_key"],
                "distance": match["distance"],
                "tier": tier,
                "card_number_agreement": agreement,
                "reference_card_number": match.get("card_number"),
                "parser_card_number": row.get("card_number"),
            }

    return analysis


def write_result(parse_id: int, market_listing_id: int, analysis: dict) -> None:
    """
    Write image_analysis back to listing_parses.
    Also sets is_junk=true and junk_reason if junk was detected.
    Never clears an existing is_junk=true set by the title parser.

    If a phash match was found:
      - auto_accept tier → only fills listing_parses.matched_card_id if it was
        previously NULL (never overwrites an existing title-parser match —
        phash is corroborating evidence here, not an override authority yet).
        Always inserts/updates a row in listing_card_matches regardless, so
        the match is recorded even when it didn't win the matched_card_id slot.
        EXCEPTION: if the phash match's card_number conflicts with the
        parser's card_number (card_number_agreement == "conflict"), the match
        is demoted to candidate instead — this is the session #31
        reprint/identical-artwork mitigation (priority #2). A low phash
        distance is not enough on its own when an independent signal
        actively disagrees; auto_accept should only fire when nothing
        contradicts it.
      - candidate tier → queued in parse_review_queue for manual review,
        does not touch matched_card_id. The reviewer note flags a
        card_number conflict explicitly when present, since that's the
        strongest signal for the reprint-collision failure mode.
      - no_match → nothing written beyond the image_analysis jsonb itself.
    """
    update: dict = {"image_analysis": analysis}  # pass dict; Supabase client serializes to jsonb
    if analysis.get("junk_detected"):
        signals = ",".join(analysis.get("junk_signals", []))
        update["is_junk"] = True
        update["junk_reason"] = f"image:{signals}"

    phash_match = analysis.get("phash_match")
    if phash_match:
        tier = phash_match["tier"]
        distance = phash_match["distance"]
        agreement = phash_match.get("card_number_agreement", "not_found")
        # Demote auto_accept -> candidate on card_number conflict. See
        # docstring above — this never *promotes* a candidate, it only ever
        # makes the outcome more conservative.
        effective_tier = "candidate" if (tier == "auto_accept" and agreement == "conflict") else tier
        # Confidence is a simple linear mapping from hamming distance over a
        # 64-bit hash — a starting heuristic, not a calibrated probability.
        # Revisit once we have real-world distance distributions to tune against.
        confidence = round(max(0.0, 1 - (distance / 64)), 4)

        if effective_tier in ("auto_accept", "candidate"):
            existing_match = (
                supabase.table("listing_card_matches")
                .select("id, match_method")
                .eq("market_listing_id", market_listing_id)
                .execute()
            )
            if not existing_match.data:
                # Only insert when no match exists yet for this listing at all —
                # listing_card_matches enforces one row per market_listing_id,
                # so phash must never clobber an existing parser_worker /
                # reparse_listings match. It can only fill a true gap.
                supabase.table("listing_card_matches").insert({
                    "market_listing_id": market_listing_id,
                    "pokemon_card_id": phash_match["pokemon_card_id"],
                    "match_method": "phash_image",
                    "match_confidence": confidence,
                    "evidence_json": {
                        "distance": distance,
                        "card_key": phash_match["card_key"],
                        "tier": tier,
                        "effective_tier": effective_tier,
                        "card_number_agreement": agreement,
                    },
                }).execute()

        if effective_tier == "auto_accept":
            # Only fill the gap — never overwrite an existing match.
            update["matched_card_id"] = phash_match["pokemon_card_id"]
            update["match_confidence"] = confidence
            current = supabase.table("listing_parses").select("matched_card_id").eq("id", parse_id).execute()
            if current.data and current.data[0]["matched_card_id"] is not None:
                update.pop("matched_card_id", None)
                update.pop("match_confidence", None)
        elif effective_tier == "candidate":
            note = (
                f"phash candidate match: card_id={phash_match['pokemon_card_id']} "
                f"({phash_match['card_key']}), distance={distance}"
            )
            if tier == "auto_accept" and agreement == "conflict":
                note = (
                    f"phash {tier} match DEMOTED to candidate due to card_number "
                    f"conflict: card_id={phash_match['pokemon_card_id']} "
                    f"({phash_match['card_key']}), distance={distance}, "
                    f"reference card_number={phash_match.get('reference_card_number')!r} "
                    f"vs parser card_number={phash_match.get('parser_card_number')!r} "
                    f"(likely reprint/identical-artwork collision)"
                )
            elif agreement == "conflict":
                note += " — NOTE: card_number conflict with parser (possible reprint/identical-artwork collision)"
            supabase.table("parse_review_queue").upsert({
                "listing_parse_id": parse_id,
                "review_status": "pending",
                "reviewer_note": note,
            }, on_conflict="listing_parse_id").execute()

    supabase.table("listing_parses").update(update).eq("id", parse_id).execute()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info(f"Image analysis worker starting — batch_size={BATCH_SIZE}")

    reference_library = fetch_reference_phashes()
    log.info(f"Loaded {len(reference_library)} reference phashes")
    if not reference_library:
        log.warning(
            "reference_phashes table is empty — phash matching will be "
            "skipped entirely this run. Run sync_reference_phashes.py locally "
            "to populate it from the manifest."
        )

    batch = fetch_batch()
    log.info(f"Rows to process: {len(batch)}")

    confirmed = conflicts = junk_flagged = errors = 0
    phash_auto_accepted = phash_candidates = 0

    for row in batch:
        parse_id = row["id"]
        market_listing_id = row["market_listing_id"]
        try:
            analysis = process_row(row, reference_library)
            write_result(parse_id, market_listing_id, analysis)

            match = analysis.get("card_number_match")
            if match == "confirm":
                confirmed += 1
            elif match == "conflict":
                conflicts += 1
            if analysis.get("junk_detected"):
                junk_flagged += 1

            phash_tier = (analysis.get("phash_match") or {}).get("tier")
            if phash_tier == "auto_accept":
                phash_auto_accepted += 1
            elif phash_tier == "candidate":
                phash_candidates += 1

            time.sleep(0.05)  # light throttle on image CDN

        except Exception as e:
            log.error(f"Error on parse_id={parse_id}: {e}", exc_info=True)
            errors += 1

    log.info(
        f"Complete — confirmed={confirmed} conflicts={conflicts} "
        f"junk_flagged={junk_flagged} errors={errors} "
        f"phash_auto_accepted={phash_auto_accepted} "
        f"phash_candidates={phash_candidates} "
        f"total={len(batch)}"
    )


if __name__ == "__main__":
    main()