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
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
BATCH_SIZE = int(os.environ.get("IMAGE_BATCH_SIZE", "150"))
IMAGE_TIMEOUT = int(os.environ.get("IMAGE_TIMEOUT_SECONDS", "10"))

# Junk thresholds
MIN_DIMENSION_PX = 80    # smaller than this → placeholder/broken image
LANDSCAPE_RATIO = 1.3    # w/h > this → landscape → likely bulk lot or scene photo

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
    """Strip leading zeros for comparison: '025' → '25', 'TG01' → None."""
    if not raw:
        return None
    m = CARD_NUMBER_RE.match(raw)
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


# ── Row processing ────────────────────────────────────────────────────────────

def process_row(row: dict) -> dict:
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
    analysis.update({
        "download_success": True,
        "image_width": w,
        "image_height": h,
        "aspect_ratio": round(w / h, 4),
        "phash": compute_phash(img),
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

    return analysis


def write_result(parse_id: int, analysis: dict) -> None:
    """
    Write image_analysis back to listing_parses.
    Also sets is_junk=true and junk_reason if junk was detected.
    Never clears an existing is_junk=true set by the title parser.
    """
    update: dict = {"image_analysis": json.dumps(analysis)}
    if analysis.get("junk_detected"):
        signals = ",".join(analysis.get("junk_signals", []))
        update["is_junk"] = True
        update["junk_reason"] = f"image:{signals}"
    supabase.table("listing_parses").update(update).eq("id", parse_id).execute()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info(f"Image analysis worker starting — batch_size={BATCH_SIZE}")

    batch = fetch_batch()
    log.info(f"Rows to process: {len(batch)}")

    confirmed = conflicts = junk_flagged = errors = 0

    for row in batch:
        parse_id = row["id"]
        try:
            analysis = process_row(row)
            write_result(parse_id, analysis)

            match = analysis.get("card_number_match")
            if match == "confirm":
                confirmed += 1
            elif match == "conflict":
                conflicts += 1
            if analysis.get("junk_detected"):
                junk_flagged += 1

            time.sleep(0.05)  # light throttle on image CDN

        except Exception as e:
            log.error(f"Error on parse_id={parse_id}: {e}", exc_info=True)
            errors += 1

    log.info(
        f"Complete — confirmed={confirmed} conflicts={conflicts} "
        f"junk_flagged={junk_flagged} errors={errors} "
        f"total={len(batch)}"
    )


if __name__ == "__main__":
    main()
