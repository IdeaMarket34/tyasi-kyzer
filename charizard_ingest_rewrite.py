import argparse
import base64
import csv
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

import requests
from dotenv import load_dotenv
from supabase import Client, create_client


load_dotenv()

EBAY_CLIENT_ID = os.environ["EBAY_CLIENT_ID"]
EBAY_CLIENT_SECRET = os.environ["EBAY_CLIENT_SECRET"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

PROMO_RE = re.compile(r"\b(?:swsh|svp|sm|xy|bw)\s*[-#]?\s*(\d{1,4})\b", re.I)
FRACTION_RE = re.compile(r"\b([a-z]{0,3}\d{1,4})\s*/\s*([a-z]{0,3}\d{1,4})\b", re.I)
GRADE_RE = re.compile(r"\b(psa|bgs|cgc|sgc)\s*(\d{1,2}(?:\.\d)?)\b", re.I)
# Matches EN and JP set codes found in listing titles.
# JP codes: sv\d{1,2}[a-z] covers sv4a, sv1a, sv3a etc. (the old sv\d{1,3} only matched sv3 not sv4a).
# s\d{1,2}[a-z]? covers s4a, s9, s12a (Sword & Shield era JP sets).
# m\d[a-z]? covers m2 (Inferno X) and m2a (Mega Dream ex).
# cll/clk are the 2023 JP Classic deck codes.
SET_CODE_RE = re.compile(
    r"\b(sv\d{1,2}[a-z]?|svp|m\d[a-z]?|s\d{1,2}[a-z]?|cll|clk"
    r"|swsh\d*|swsh|sm\d*|sm|xy\d*|xy|bw\d+|bw|dp|pl|neo\d|base|ecard"
    r"|pfl|asc|cp6)\b",
    re.I
)

# Bare/standalone card-number signals, used as a fallback when there's no
# "/total" fraction in the title (e.g. promo cards). These are intentionally
# anchored to explicit cues (#, a grade-style alpha+digit code, or proximity
# to "ex"/"promo") rather than scanning the whole title for any digit run,
# which previously caused years and listing-id prefixes to be misread as
# card numbers (session #11).
HASH_NUMBER_RE = re.compile(r"#\s*([a-z]{0,3}\d{1,4})\b", re.I)
ALPHA_CARD_CODE_RE = re.compile(r"\b([a-z]{2,3}\d{1,4})\b", re.I)
GRADE_PREFIX_WORDS = {"psa", "bgs", "cgc", "sgc"}

# Alpha prefixes that appear in eBay listing titles but are NOT card-number
# prefixes in the Pokemon TCG catalog (confirmed against pokemon_cards table).
# Blocking these prevents extract_bare_card_number() from returning garbage.
#   HP  = hit point value ("HP500 Charizard")
#   NT  = Japanese card-condition label (Near-Top)
#   NO  = "No.006" card-name convention, not a card number
#   SI  = slab/cert identifier suffix
#   SS  = non-TCG set code
#   EX  = ambiguous / not in catalog as prefix (EX era cards use plain numbers)
#   C   = non-TCG / fanart label
#   E   = Topps/non-TCG
#   CSV = Chinese set identifier, not a card number
#   SMP = Japanese promo set code (no Charizard cards in catalog with this prefix)
BLOCKED_ALPHA_PREFIXES = {
    "hp", "nt", "no", "si", "ss", "ex", "c", "e", "csv", "smp",
}
NEAR_VARIANT_NUMBER_RE = re.compile(r"\b(?:ex|gx|v|vmax|vstar)\s+(\d{2,3})\b", re.I)
NEAR_PROMO_CONTEXT_NUMBER_RE = re.compile(
    r"\b(\d{2,3})\b\s+(?:mega evolution|black star|promo)", re.I
)

JUNK_PATTERNS = [
    r"\bproxy\b",
    r"\bcustom\b",
    r"\bmetal card\b",
    r"\bgift\s*/?\s*display\b",
    r"\binspired\b",
    r"\bjumbo\b",
    r"\bcoin[s]?\b",
    r"\blot\b",
    r"\b\d+\s*pcs\b",
    r"\bsticker\b",
    r"\bdeck box\b",
    r"\bsleeve[s]?\b",
    r"\bplaymat\b",
    r"\bpoker playing card\b",
    r"\bchoose your\b",   # multi-card lot listings ("choose your card/exact card/mini set")
    r"\bfanart\b",        # non-TCG fan-made cards
    # Sealed / non-individual-card products — these can never match a single
    # pokemon_cards row, so they were piling up in the unmatched-non-junk
    # bucket instead of being filtered out. Added session #27.
    r"\belite trainer box\b",
    r"\bbooster box\b",
    r"\bbooster pack\b",
    r"\bbooster bundle\b",
    r"\bdisplay case\b",
    r"\bsealed box\b",
    r"\bcollection box\b",
    r"\bportfolio\b",
    r"\bbinder\b",
    r"\btin\b",
    r"\bgold plated\b",   # novelty/non-genuine cards, not real TCG cards
]

KNOWN_SET_NAME_PATTERNS = [
    "paldean fates",
    "obsidian flames",
    "pokemon 151",
    "151",
    "team rocket",
    "gym challenge",
    "brilliant stars",
    "burning shadows",
    "champion's path",
    "champions path",
    "skyridge",
    "power keepers",
    "star birth",
    "vmax climax",
    "freeze bolt",
    "hidden fates",
    "shining fates",
    "crown zenith",
    "pokemon go",
    "unbroken bonds",
    "cosmic eclipse",
    "dragon majesty",
    "flashfire",
    "evolutions",
    "generations",
    "team up",
    "base set 2",
    "base set",
    "base",
    "legendary collection",
    "expedition base set",
    "supreme victors",
    "stormfront",
    "plasma storm",
    "neo destiny",
    # English sets missing from original list
    "darkness ablaze",
    "celebrations",
    "celebration set",   # "25th Anniversary Celebration Set Holo" — subset of Celebrations
    "classic collection",   # Celebrations: Classic Collection subset → cel
    "lost origin",
    "pokemon mew",          # "Pokemon MEW EN" alt title for Scarlet & Violet 151
    "mew en",               # same
    "rocket 1st edition",   # EN Team Rocket set titles often omit "team"
    "pokemon rocket",       # same — "2000 Pokemon Rocket #4 Dark Charizard"
    "dark charizard",       # uniquely EN Team Rocket; no other EN set has this card
    "pokemon unlimited",    # "1999 Pokemon Unlimited #4/102" → Base Set
    "obf",                  # Obsidian Flames abbreviation used in graded-card titles
    # English Mega Evolution era sets
    "phantasmal flames",
    "ascended heroes",
    # Japanese set names — modern high-value sets
    "shiny treasure ex",
    "ruler of the black flame",
    "vstar universe",
    "shiny star v",
    "inferno x",
    "mega dream ex",
    "mega dream attack",     # alt title used in eBay listings for m2a
    # JP Classic deck — detected via the tag-team card name unique to the CLL deck
    "classic: charizard",    # "Pokemon TCG Classic: Charizard & Ho-Oh ex Deck"
    "charizard & ho-oh ex",
]

SET_NAME_NORMALIZATIONS = {
    "champion's path": "champions_path",
    "champions path": "champions_path",
    "pokemon 151": "151",
    "151": "151",
    "base set": "base",
    "base": "base",
    "base set 2": "base_set_2",
    "team rocket": "team_rocket",
    "gym challenge": "gym_challenge",
    "paldean fates": "paldean_fates",
    "obsidian flames": "obsidian_flames",
    "brilliant stars": "brilliant_stars",
    "burning shadows": "burning_shadows",
    "power keepers": "power_keepers",
    "star birth": "s9",        # JP-only set (EN equivalent: Brilliant Stars)
    "vmax climax": "s8b",      # JP-only set (no direct EN equivalent)
    "freeze bolt": "freeze_bolt",
    "skyridge": "skyridge",
    "hidden fates": "hidden_fates",
    "shining fates": "shining_fates",
    "crown zenith": "crown_zenith",
    "pokemon go": "pokemon_go",
    "unbroken bonds": "unbroken_bonds",
    "cosmic eclipse": "cosmic_eclipse",
    "dragon majesty": "dragon_majesty",
    "flashfire": "flashfire",
    "evolutions": "evolutions",
    "generations": "generations",
    "team up": "team_up",
    "legendary collection": "legendary_collection",
    "expedition base set": "expedition_base_set",
    "supreme victors": "supreme_victors",
    "stormfront": "stormfront",
    "plasma storm": "plasma_storm",
    "neo destiny": "neo_destiny",
    # English sets missing from original list
    "darkness ablaze": "daa",
    "celebrations": "cel",
    "celebration set": "cel",    # "25th Anniversary Celebration Set Holo" → same set as celebrations
    "classic collection": "cel",    # Celebrations: Classic Collection subset; card #4 lives in cel
    "lost origin": "lor",
    "pokemon mew": "151",           # "Pokemon MEW EN" → Scarlet & Violet 151
    "mew en": "151",                # same
    "rocket 1st edition": "tr",     # EN Team Rocket; "team" often omitted in titles
    "pokemon rocket": "tr",         # same
    "dark charizard": "tr",         # uniquely EN Team Rocket — no other EN set has this card
    "pokemon unlimited": "bs",      # "1999 Pokemon Unlimited #4/102" → Base Set
    "obf": "obsidianflames",        # Obsidian Flames abbreviation
    # English Mega Evolution era sets
    "phantasmal flames": "pfl",
    "ascended heroes": "asc",
    # Japanese set names → their canonical JP set_keys
    "shiny treasure ex": "sv4a",
    "ruler of the black flame": "sv3",
    "vstar universe": "s12a",
    "shiny star v": "s4a",
    "inferno x": "m2",
    "mega dream ex": "m2a",
    "mega dream attack": "m2a",      # alt eBay title for MEGA Dream ex (m2a)
    "charizard & ho-oh ex": "cll",
    "classic: charizard": "cll",     # "Pokemon TCG Classic: Charizard & Ho-Oh ex Deck"
}

SET_CANONICAL_OVERRIDES = {
    "base": {"set_key": "bs", "set_name": "Base"},
    "base_set_2": {"set_key": "b2", "set_name": "Base Set 2"},
    "team_rocket": {"set_key": "tr", "set_name": "Team Rocket"},
    "gym_challenge": {"set_key": "g2", "set_name": "Gym Challenge"},
    "skyridge": {"set_key": "sk", "set_name": "Skyridge"},
    "power_keepers": {"set_key": "pk", "set_name": "Power Keepers"},
    "champions_path": {"set_key": "cpa", "set_name": "Champions Path"},
    "brilliant_stars": {"set_key": "brs", "set_name": "Brilliant Stars"},
    "burning_shadows": {"set_key": "bus", "set_name": "Burning Shadows"},
    "151": {"set_key": "151", "set_name": "151"},
    "paldean_fates": {"set_key": "paldeanfates", "set_name": "Paldean Fates"},
    "obsidian_flames": {"set_key": "obsidianflames", "set_name": "Obsidian Flames"},
    "hidden_fates": {"set_key": "hif", "set_name": "Hidden Fates"},
    "shining_fates": {"set_key": "shf", "set_name": "Shining Fates"},
    "crown_zenith": {"set_key": "crz", "set_name": "Crown Zenith"},
    "pokemon_go": {"set_key": "pgo", "set_name": "Pokémon GO"},
    "unbroken_bonds": {"set_key": "unb", "set_name": "Unbroken Bonds"},
    "cosmic_eclipse": {"set_key": "cec", "set_name": "Cosmic Eclipse"},
    "dragon_majesty": {"set_key": "drm", "set_name": "Dragon Majesty"},
    "flashfire": {"set_key": "flf", "set_name": "Flashfire"},
    "evolutions": {"set_key": "evo", "set_name": "Evolutions"},
    "generations": {"set_key": "gen", "set_name": "Generations"},
    "team_up": {"set_key": "teu", "set_name": "Team Up"},
    "legendary_collection": {"set_key": "lc", "set_name": "Legendary Collection"},
    "expedition_base_set": {"set_key": "ex", "set_name": "Expedition Base Set"},
    "supreme_victors": {"set_key": "sv", "set_name": "Supreme Victors"},
    "stormfront": {"set_key": "sf", "set_name": "Stormfront"},
    "plasma_storm": {"set_key": "pls", "set_name": "Plasma Storm"},
    "neo_destiny": {"set_key": "n4", "set_name": "Neo Destiny"},
    "prsw": {"set_key": "prsw", "set_name": "SWSH Black Star Promos"},
    "prsv": {"set_key": "prsv", "set_name": "Scarlet Violet Black Star Promos"},
    "prsm": {"set_key": "prsm", "set_name": "SM Black Star Promos"},
    "prxy": {"set_key": "prxy", "set_name": "XY Black Star Promos"},
    "prbw": {"set_key": "prbw", "set_name": "BW Black Star Promos"},
}

KNOWN_CARD_TOTALS = {
    "17", "18", "20", "21", "25", "30", "32", "56", "68", "73", "78", "82", "83", "87", "91",
    "94", "95", "97", "100", "101", "102", "105", "106", "107", "108", "110", "111",
    "112", "113", "122", "124", "130", "132", "135", "146", "147", "149", "159", "165",
    "172", "181", "185", "189", "197", "198", "199", "202", "203", "204", "211", "214",
    "215", "217", "230", "234", "236", "248", "307",
    # JP-specific set totals
    "80",   # M2 Inferno X
    "190",  # sv4a Shiny Treasure ex, s4a Shiny Star V
    "193",  # M2a Mega Dream ex
}

QUERIES = [
    "Charizard Pokemon card",
    "Charizard ex Pokemon",
    "Charizard promo Pokemon",
    "Japanese Charizard Pokemon",
    "Mega Charizard Pokemon",
]


@dataclass
class ParsedTitle:
    raw_title: str
    normalized_title: str
    pokemon_name: Optional[str]
    set_code: Optional[str]
    card_number_guess: Optional[str]
    card_number: Optional[str]
    card_total_guess: Optional[str]
    card_fraction_norm: Optional[str]
    promo_code: Optional[str]
    grade_company: Optional[str]
    grade_value: Optional[float]
    language: Optional[str]
    variant: Optional[str]
    is_junk: bool
    junk_reason: Optional[str]


def normalize_text(title: str) -> str:
    t = (title or "").lower()
    t = t.replace("pokémon", "pokemon")
    t = t.replace("blaines", "blaine's")
    t = t.replace("lances", "lance's")
    t = re.sub(r"[^\w/#&+\-\s:.']", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def slugify_set_name(value: str) -> str:
    value = normalize_text(value)
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value


def normalize_set_key(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = normalize_text(value)
    value = value.replace(" ", "_")
    value = re.sub(r"[^a-z0-9_]+", "", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or None


def _normalize_card_part(part: str) -> str:
    part = (part or "").strip().upper()
    m = re.match(r"^([A-Z]{0,3})(\d{1,4})$", part)
    if not m:
        return part.lower()
    prefix = m.group(1)
    number = str(int(m.group(2)))
    return f"{prefix}{number}" if prefix else number


def _looks_like_year(token: str) -> bool:
    digits = re.sub(r"\D", "", token or "")
    if not digits:
        return False
    try:
        n = int(digits)
    except ValueError:
        return False
    return 1996 <= n <= 2035


def _split_compact_fraction_token(token: str) -> Optional[Tuple[str, str]]:
    token = (token or "").strip()
    if not token.isdigit():
        return None

    if len(token) < 3 or len(token) > 6:
        return None

    # A 4-digit token in the modern TCG era range is almost always a year
    # (e.g. "2025"), not a numerator/total pair. Splitting it produced
    # false matches like "2025" -> "20"/"25" when the real card number
    # (e.g. "#023") appeared later in the title. See session #11 writeup.
    if len(token) == 4 and _looks_like_year(token):
        return None

    candidates = []

    for right_len in (2, 3):
        if len(token) <= right_len:
            continue
        left = token[:-right_len]
        right = token[-right_len:]

        left_norm = str(int(left))
        right_norm = str(int(right))

        if right_norm in KNOWN_CARD_TOTALS:
            candidates.append((left_norm, right_norm))

    if not candidates:
        return None

    candidates.sort(key=lambda pair: (len(pair[1]), int(pair[1])), reverse=True)
    return candidates[0]


def extract_fraction_fields(value: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    if not value:
        return None, None, None, None

    m = FRACTION_RE.search(value)
    if m:
        raw_left = m.group(1)
        raw_right = m.group(2)
        left_norm = _normalize_card_part(raw_left)
        right_norm = _normalize_card_part(raw_right)
        raw = f"{raw_left}/{raw_right}"
        fraction_norm = f"{left_norm}/{right_norm}"
        return raw, left_norm, right_norm, fraction_norm

    for token in re.findall(r"\b\d{3,6}\b", value):
        split_token = _split_compact_fraction_token(token)
        if split_token:
            left_norm, right_norm = split_token
            raw = token
            fraction_norm = f"{left_norm}/{right_norm}"
            return raw, left_norm, right_norm, fraction_norm

    cleaned = value.strip()
    return cleaned, None, None, None


def extract_card_number_aspect(value: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Like extract_fraction_fields(), but for eBay's structured "Card Number"
    aspect specifically.

    That aspect is usually a bare promo/card number with no "/total"
    delimiter (e.g. "030"), not a real fraction. Running it through
    extract_fraction_fields()'s compact-token fallback incorrectly split
    bare 3-digit numbers into a fake numerator/total pair whenever the last
    2-3 digits happened to match a known set total — e.g. "030" -> "0"/"30"
    instead of being read as plain card #30. That fallback exists to handle
    ambiguous *title* text; title parsing already avoids calling it (see
    parse_listing_title), but the aspect-data path called
    extract_fraction_fields() directly and reintroduced the same bug.
    Fixed session #27 — only run the fraction splitter when a literal "/"
    is actually present in the aspect value; otherwise treat the whole
    value as a single bare card number.
    """
    if not value:
        return None, None, None, None
    if "/" in value:
        return extract_fraction_fields(value)
    cleaned = value.strip()
    if not cleaned:
        return None, None, None, None
    return cleaned, _normalize_card_part(cleaned), None, None


def extract_bare_card_number(t: str) -> Optional[str]:
    """Fallback card-number extraction for titles with no '/total' fraction.

    Anchored to explicit cues so it can't grab an unrelated number (a year,
    a listing-id prefix, a PSA/CGC grade) the way the old blanket
    "any 3-6 digit run in the title" fallback did.
    """
    if not t:
        return None

    m = HASH_NUMBER_RE.search(t)
    if m and not _looks_like_year(m.group(1)):
        return _normalize_card_part(m.group(1))

    for m in ALPHA_CARD_CODE_RE.finditer(t):
        token = m.group(1)
        prefix_match = re.match(r"^[a-z]+", token, re.I)
        if prefix_match:
            prefix_lower = prefix_match.group(0).lower()
            if prefix_lower in GRADE_PREFIX_WORDS:
                continue
            if prefix_lower in BLOCKED_ALPHA_PREFIXES:
                continue
        if _looks_like_year(token):
            continue
        return _normalize_card_part(token)

    m = NEAR_VARIANT_NUMBER_RE.search(t)
    if m and not _looks_like_year(m.group(1)):
        return _normalize_card_part(m.group(1))

    m = NEAR_PROMO_CONTEXT_NUMBER_RE.search(t)
    if m and not _looks_like_year(m.group(1)):
        return _normalize_card_part(m.group(1))

    return None


def detect_junk(t: str) -> Tuple[bool, Optional[str]]:
    for pat in JUNK_PATTERNS:
        if re.search(pat, t):
            return True, pat
    return False, None


def extract_promo_code(t: str) -> Optional[str]:
    if not t:
        return None

    promo_patterns = [
        r"\b(swsh)\s*[-#:]?\s*(\d{1,3})\b",
        r"\b(svp)\s*[-#:]?\s*(\d{1,3})\b",
        r"\b(sm)\s*[-#:]?\s*(\d{1,3})\b",
        r"\b(xy)\s*[-#:]?\s*(\d{1,3})\b",
        r"\b(bw)\s*[-#:]?\s*(\d{1,3})\b",
        r"\b(dp)\s*[-#:]?\s*(\d{1,3})\b",
    ]

    for pattern in promo_patterns:
        m = re.search(pattern, t, re.I)
        if m:
            return f"{m.group(1).lower()}{m.group(2).zfill(3)}"

    return None


def extract_grade(t: str) -> Tuple[Optional[str], Optional[float]]:
    m = GRADE_RE.search(t)
    if not m:
        return None, None
    return m.group(1).upper(), float(m.group(2))


ERA_FULL_NAME_TO_PROMO_SET = [
    (r"sun\s*&?\s*moon|sun and moon", "prsm"),
    (r"sword\s*&?\s*shield|sword and shield", "prsw"),
    (r"diamond\s*&?\s*pearl|diamond and pearl", "prdpp"),
    (r"black\s*&?\s*white|black and white", "prblw"),
    (r"scarlet\s*&?\s*violet|scarlet and violet", "prsv"),
    (r"\bxy\b|x\s*&?\s*y\b", "prxy"),
]


def detect_set_from_text(t: str) -> Optional[str]:
    if not t:
        return None

    promo = extract_promo_code(t)
    if promo:
        if promo.startswith("swsh"):
            return "prsw"
        if promo.startswith("svp"):
            return "prsv"
        if promo.startswith("sm"):
            return "prsm"
        if promo.startswith("xy"):
            return "prxy"
        if promo.startswith("bw"):
            # Catalog's populated BW promo row uses set_key "prblw"
            # (93 reference cards); "prbw" is an empty/orphaned duplicate.
            return "prblw"
        if promo.startswith("dp"):
            # Catalog's populated DP promo row uses set_key "prdpp"
            # (51 reference cards).
            return "prdpp"

    # Promo titles often spell the era out ("Sun & Moon Black Star Promo",
    # "Diamond & Pearl Black Star Promo") rather than using the literal
    # abbreviation extract_promo_code looks for. Check those explicitly so
    # an older era doesn't fall through to the modern-era default below.
    if re.search(r"\bpromo", t):
        for pattern, set_key in ERA_FULL_NAME_TO_PROMO_SET:
            if re.search(pattern, t):
                return set_key

        if re.search(r"\bblack star promos?\b", t):
            return "prsv"

    # Modern Scarlet & Violet / Mega Evolution Black Star Promos are often
    # written with no era marker, and "MEP" (Mega Evolution Promo) titles
    # sometimes skip the literal word "promo" entirely ("MEP EN #023"). They
    # already have 161 reference cards under set_key "prsv" - resolve there
    # directly rather than auto-creating a new (likely duplicate) set.
    # "BSP" (Black Star Promo) titles follow the same pattern.
    if re.search(r"\bmega evolution\b", t) or re.search(r"\bmep\b", t) or re.search(r"\bbsp\b", t):
        return "prsv"

    # JP anniversary sets — language-gated so EN "25th Anniversary Celebration Set"
    # titles don't get routed here (they have no JP signal and fall through to
    # the "celebration set" → cel named-pattern check below instead).
    _is_jp = bool(re.search(r"japanese|\bjpn?\b", t))
    if _is_jp and re.search(r"\b25th\b", t):
        return "s8a"
    if _is_jp and (re.search(r"\b20th\b", t) or re.search(r"\bcp6\b", t)):
        return "cp6"

    for pattern in sorted(KNOWN_SET_NAME_PATTERNS, key=len, reverse=True):
        if pattern == "151":
            # "151" as a known set name (English Scarlet & Violet "151") was
            # matching as a blind substring, which also fires on any card
            # number written as "X/151" where 151 is just that card's total
            # count — e.g. a Chinese CSM1aC Charizard GX numbered "004/151"
            # was misidentified as the English 151 set purely because its
            # total happened to be 151. Require it not be the right side of
            # a "/151" fraction. Fixed session #27.
            if re.search(r"(?<!/)\b151\b", t):
                normalized = SET_NAME_NORMALIZATIONS.get(pattern, slugify_set_name(pattern))
                override = SET_CANONICAL_OVERRIDES.get(normalized)
                if override:
                    return override["set_key"]
                return normalized
            continue
        if pattern in t:
            normalized = SET_NAME_NORMALIZATIONS.get(pattern, slugify_set_name(pattern))
            override = SET_CANONICAL_OVERRIDES.get(normalized)
            if override:
                return override["set_key"]
            return normalized

    # "UPC" (Ultra Premium Collection) promo cards route to prsv only as a
    # last-resort fallback after all named-set patterns have been checked,
    # to avoid clobbering titles like "UPC Darkness Ablaze Charizard VMAX".
    if re.search(r"\bupc\b", t):
        return "prsv"

    m = SET_CODE_RE.search(t)
    if m:
        return normalize_set_key(m.group(1))

    return None


def extract_language(t: str) -> str:
    # "jpn" is extremely common in eBay titles ("JPN", "Jpn") — catch it alongside "jp".
    if "japanese" in t or re.search(r"\bjpn?\b", t):
        return "ja"
    return "en"


def extract_variant(t: str) -> Optional[str]:
    variant_patterns = [
        (r"\bvstar\b", "vstar"),
        (r"\bvmax\b", "vmax"),
        (r"\bgx\b", "gx"),
        (r"\bex\b", "ex"),
        (r"\bdark\b", "dark"),
        (r"\bshining\b", "shining"),
        (r"\breverse holo\b", "reverse_holo"),
        (r"\bholo\b", "holo"),
        (r"\bpromo\b", "promo"),
        (r"\bv\b", "v"),
    ]
    for pattern, value in variant_patterns:
        if re.search(pattern, t):
            return value
    return None

def has_strong_single_card_signal(parsed, title_norm: str) -> bool:
    if parsed.promo_code:
        return True
    if parsed.card_fraction_norm or parsed.card_number:
        return True
    if parsed.grade_company or parsed.grade_value is not None:
        return True
    if re.search(r"\bpromo\b", title_norm):
        return True
    return False

def parse_listing_title(title: str) -> ParsedTitle:
    t = normalize_text(title)
    is_junk, junk_reason = detect_junk(t)

    # Only trust a real "/total" fraction here. The old code ran the
    # generic title text through extract_fraction_fields()'s compact-blob
    # fallback too, which scans the *entire* title for any 3-6 digit run -
    # that's what was grabbing years ("2025"->"20"/"25") and inventing fake
    # totals for standalone promo numbers ("030"->"0"/"30"). Bare/promo-style
    # numbers are handled by the cue-anchored extract_bare_card_number()
    # fallback below instead.
    fraction_match = FRACTION_RE.search(t)
    if fraction_match:
        raw_left, raw_right = fraction_match.group(1), fraction_match.group(2)
        card_norm = _normalize_card_part(raw_left)
        card_total = _normalize_card_part(raw_right)
        card_raw = f"{raw_left}/{raw_right}"
        card_fraction = f"{card_norm}/{card_total}"
    else:
        card_raw = card_norm = card_total = card_fraction = None
        bare = extract_bare_card_number(t)
        if bare:
            card_raw = bare
            card_norm = bare

    grade_company, grade_value = extract_grade(t)

    pokemon_name = "charizard" if "charizard" in t else None

    # If the title doesn't mention "charizard" at all, the parser already
    # correctly determined this isn't a Charizard listing (match_reference_card
    # skips matching when pokemon_name is None) — but nothing was marking it
    # junk, so these piled up forever in the "unmatched, non-junk" bucket
    # instead of being filtered out. Preserve a more specific junk_reason if
    # one was already set (e.g. "lot", "proxy") rather than overwriting it.
    # Added session #27.
    if not pokemon_name and not is_junk:
        is_junk = True
        junk_reason = "non_charizard"

    return ParsedTitle(
        raw_title=title,
        normalized_title=t,
        pokemon_name=pokemon_name,
        set_code=detect_set_from_text(t),
        card_number_guess=card_raw,
        card_number=card_norm,
        card_total_guess=card_total,
        card_fraction_norm=card_fraction,
        promo_code=extract_promo_code(t),
        grade_company=grade_company,
        grade_value=grade_value,
        language=extract_language(t),
        variant=extract_variant(t),
        is_junk=is_junk,
        junk_reason=junk_reason,
    )


def extract_aspects(detail: dict) -> Dict[str, Optional[str]]:
    aspects = detail.get("localizedAspects") or []
    result = {
        "set_name": None,
        "card_number_raw": None,
        "language": None,
        "game": None,
        "character": None,
        "rarity": None,
        "grade_company": None,
        "grade_value": None,
        "is_jumbo": False,
        "is_oversize": False,
        "is_proxy": False,
        "is_custom": False,
    }

    for a in aspects:
        name = (a.get("name") or "").strip().lower()
        value = (a.get("value") or "").strip()
        if not name or not value:
            continue

        value_norm = normalize_text(value)

        if name == "set":
            result["set_name"] = value
        elif name == "card number":
            result["card_number_raw"] = value
        elif name == "language":
            result["language"] = value
        elif name == "game":
            result["game"] = value
        elif name == "character":
            result["character"] = value
        elif name == "rarity":
            result["rarity"] = value
        elif name in {"professional grader", "grader", "grading company"}:
            result["grade_company"] = value.upper()
        elif name in {"grade", "card condition"}:
            grade_match = re.search(r"\b(\d{1,2}(?:\.\d)?)\b", value)
            if grade_match:
                result["grade_value"] = float(grade_match.group(1))

        if "jumbo" in value_norm:
            result["is_jumbo"] = True
        if "oversize" in value_norm or "oversized" in value_norm:
            result["is_oversize"] = True
        if "proxy" in value_norm:
            result["is_proxy"] = True
        if "custom" in value_norm:
            result["is_custom"] = True

    return result


def title_case_set_name(set_key: str) -> str:
    return " ".join(part.capitalize() for part in set_key.replace("_", " ").split())


def parse_aliases_field(value) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def infer_set_insert_payload(set_code: str, aspect_data: Optional[Dict[str, Optional[str]]] = None) -> dict:
    aspect_data = aspect_data or {}
    normalized_guess = normalize_set_key(set_code) or "unknown_set"
    override = SET_CANONICAL_OVERRIDES.get(normalized_guess)

    set_key = override["set_key"] if override else normalized_guess
    set_name = (
        aspect_data.get("set_name")
        or (override["set_name"] if override else title_case_set_name(normalized_guess))
    )

    aliases = sorted(
        {
            set_name,
            normalized_guess,
            (aspect_data.get("set_name") or "").strip(),
        } - {""}
    )

    # Infer language from the set_key: JP-format codes start with sv\d, s\d, m\d, cll, clk.
    # This prevents auto-created JP sets from being stored with language='en'.
    _jp_key_re = re.compile(r'^(sv\d|s\d|m\d|cll|clk)', re.I)
    language = "ja" if _jp_key_re.match(set_key) else "en"

    return {
        "set_key": set_key,
        "set_name": set_name,
        "series_name": None,
        "set_code": None,
        "language": language,
        "release_date": None,
        "aliases": ",".join(aliases),
    }


def get_or_create_set(set_code: Optional[str], aspect_data: Optional[Dict[str, Optional[str]]] = None) -> Optional[dict]:
    if not set_code and not (aspect_data or {}).get("set_name"):
        return None

    aspect_data = aspect_data or {}
    candidate_inputs = [set_code, aspect_data.get("set_name")]
    normalized_candidates = [normalize_set_key(x) for x in candidate_inputs if x]
    normalized_candidates = [x for x in normalized_candidates if x]

    readable_candidates = []
    if aspect_data.get("set_name"):
        readable_candidates.append(aspect_data["set_name"].strip())

    for candidate in normalized_candidates:
        result = (
            supabase.table("pokemon_sets")
            .select("id,set_key,set_name,aliases")
            .eq("set_key", candidate)
            .limit(1)
            .execute()
        )
        if result.data:
            return result.data[0]

    for readable in readable_candidates:
        result = (
            supabase.table("pokemon_sets")
            .select("id,set_key,set_name,aliases")
            .ilike("set_name", readable)
            .limit(5)
            .execute()
        )
        if result.data:
            exact = next(
                (row for row in result.data if normalize_text(row.get("set_name") or "") == normalize_text(readable)),
                None,
            )
            if exact:
                return exact

    existing = (
        supabase.table("pokemon_sets")
        .select("id,set_key,set_name,aliases")
        .limit(5000)
        .execute()
    )

    for row in existing.data or []:
        alias_values = parse_aliases_field(row.get("aliases"))
        normalized_aliases = {normalize_set_key(v) for v in alias_values if v}
        normalized_aliases.add(normalize_set_key(row.get("set_name")))
        normalized_aliases.add(normalize_set_key(row.get("set_key")))
        if any(candidate in normalized_aliases for candidate in normalized_candidates):
            return row

    insert_payload = infer_set_insert_payload(
        set_code or normalize_set_key(aspect_data.get("set_name") or "unknown_set"),
        aspect_data=aspect_data,
    )

    inserted = supabase.table("pokemon_sets").insert(insert_payload).execute()
    if inserted.data:
        return inserted.data[0]

    retry = (
        supabase.table("pokemon_sets")
        .select("id,set_key,set_name,aliases")
        .eq("set_key", insert_payload["set_key"])
        .limit(1)
        .execute()
    )
    if retry.data:
        return retry.data[0]

    return None


def match_reference_card(parsed: ParsedTitle, aspects: Optional[Dict[str, Optional[str]]] = None):
    if not parsed.pokemon_name or parsed.is_junk:
        return None

    aspects = aspects or {}
    language = (parsed.language or "en").lower()
    set_code = parsed.set_code
    card_num = parsed.card_number
    card_total = parsed.card_total_guess
    promo_code = parsed.promo_code

    if not set_code and aspects.get("set_name"):
        set_code = detect_set_from_text(normalize_text(aspects["set_name"])) or normalize_set_key(aspects["set_name"])

    if (not card_num or not card_total) and aspects.get("card_number_raw"):
        _, num, total, _ = extract_card_number_aspect(aspects["card_number_raw"])
        if num:
            card_num = num
        if total:
            card_total = total

    if promo_code:
        prefix = re.match(r"^[a-z]+", promo_code)
        digits = re.search(r"(\d+)$", promo_code)
        if prefix and digits:
            promo_result = (
                supabase.table("pokemon_cards")
                .select("id,card_key,pokemon_name,set_id,card_number_raw,card_number,total_in_set,promo_prefix,language,metadata,pokemon_sets(set_key)")
                .eq("pokemon_name", parsed.pokemon_name)
                .eq("promo_prefix", prefix.group(0).upper())
                .eq("card_number", str(int(digits.group(1))))
                .eq("language", language)
                .limit(5)
                .execute()
            )
            if len(promo_result.data or []) == 1:
                return promo_result.data[0]

    if set_code and card_num and card_total:
        set_result = (
            supabase.table("pokemon_sets")
            .select("id,set_key,set_name")
            .eq("set_key", set_code)
            .limit(1)
            .execute()
        )
        if set_result.data:
            set_id = set_result.data[0]["id"]
            card_result = (
                supabase.table("pokemon_cards")
                .select("id,card_key,pokemon_name,set_id,card_number_raw,card_number,total_in_set,promo_prefix,language,metadata,pokemon_sets(set_key)")
                .eq("pokemon_name", parsed.pokemon_name)
                .eq("set_id", set_id)
                .eq("card_number", card_num)
                .eq("total_in_set", card_total)
                .eq("language", language)
                .limit(5)
                .execute()
            )
            if len(card_result.data or []) == 1:
                return card_result.data[0]

    if card_num and card_total:
        num_result = (
            supabase.table("pokemon_cards")
            .select("id,card_key,pokemon_name,set_id,card_number_raw,card_number,total_in_set,promo_prefix,language,metadata,pokemon_sets(set_key)")
            .eq("pokemon_name", parsed.pokemon_name)
            .eq("card_number", card_num)
            .eq("total_in_set", card_total)
            .eq("language", language)
            .limit(10)
            .execute()
        )
        candidates = num_result.data or []
        if len(candidates) == 1:
            return candidates[0]

    if set_code and card_num:
        set_result = (
            supabase.table("pokemon_sets")
            .select("id,set_key,set_name")
            .eq("set_key", set_code)
            .limit(1)
            .execute()
        )
        if set_result.data:
            set_id = set_result.data[0]["id"]
            card_result = (
                supabase.table("pokemon_cards")
                .select("id,card_key,pokemon_name,set_id,card_number_raw,card_number,total_in_set,promo_prefix,language,metadata,pokemon_sets(set_key)")
                .eq("pokemon_name", parsed.pokemon_name)
                .eq("set_id", set_id)
                .eq("card_number", card_num)
                .eq("language", language)
                .limit(10)
                .execute()
            )
            candidates = card_result.data or []
            if len(candidates) == 1:
                return candidates[0]

    return None


def get_ebay_access_token() -> str:
    auth = base64.b64encode(f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()).decode()
    response = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def fetch_charizard_items(access_token: str, max_items: int = 1500) -> List[dict]:
    collected: List[dict] = []
    seen_ids = set()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }

    for query in QUERIES:
        offset = 0
        while len(collected) < max_items:
            params = {"q": query, "limit": 200, "offset": offset}
            for attempt in range(1, 6):
                response = requests.get(
                    "https://api.ebay.com/buy/browse/v1/item_summary/search",
                    headers=headers,
                    params=params,
                    timeout=30,
                )
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    wait_s = int(retry_after) if retry_after else min(2 ** attempt, 60)
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

            for item in items:
                item_id = item.get("itemId")
                if item_id and item_id not in seen_ids:
                    seen_ids.add(item_id)
                    collected.append(item)
                    if len(collected) >= max_items:
                        break

            if len(items) < 200 or len(collected) >= max_items:
                break
            offset += 200

    return collected


def load_existing_charizard_items(limit: int = 1500) -> List[dict]:
    rows: List[dict] = []
    page_size = 500
    start = 0
    while len(rows) < limit:
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
            if len(rows) >= limit:
                break
        if len(batch) < page_size:
            break
        start += page_size
    return rows[:limit]


def get_charizard_items(access_token: str, max_items: int) -> List[dict]:
    try:
        items = fetch_charizard_items(access_token, max_items=max_items)
        print(f"Fetched {len(items)} live Charizard items from eBay.")
        return items
    except Exception as e:
        print(f"Live eBay fetch failed: {e}")
        items = load_existing_charizard_items(limit=max_items)
        print(f"Loaded {len(items)} stored Charizard items from Supabase.")
        return items


def fetch_item_detail(access_token: str, item_id: str) -> dict:
    response = requests.get(
        f"https://api.ebay.com/buy/browse/v1/item/{item_id}",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def fetch_item_detail_safe(access_token: str, item_id: str, max_retries: int = 4) -> Dict[str, Optional[dict]]:
    for attempt in range(1, max_retries + 1):
        try:
            return {"item_id": item_id, "detail": fetch_item_detail(access_token, item_id), "error": None}
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status in (429, 500, 502, 503, 504) and attempt < max_retries:
                time.sleep(min(2 ** attempt, 20))
                continue
            return {"item_id": item_id, "detail": None, "error": f"http_{status}"}
        except Exception as e:
            if attempt < max_retries:
                time.sleep(min(2 ** attempt, 20))
                continue
            return {"item_id": item_id, "detail": None, "error": str(e)}


def enrich_items_with_details_concurrent(access_token: str, items: List[dict], max_workers: int = 6):
    enriched = []
    future_to_item = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for item in items:
            item_id = item.get("itemId")
            if not item_id:
                enriched.append((item, None))
                continue
            future_to_item[executor.submit(fetch_item_detail_safe, access_token, item_id)] = item
        total = len(future_to_item)
        done_count = 0
        for future in as_completed(future_to_item):
            item = future_to_item[future]
            result = future.result()
            done_count += 1
            if result["error"]:
                print(f"[{done_count}/{total}] detail failed for {item.get('itemId')}: {result['error']}")
            enriched.append((item, result["detail"]))
    return enriched


def normalize_charizard_key_from_match(matched_card: dict, parsed: "ParsedTitle") -> str:
    """Build the group/identity key from the matched reference card rather than
    from per-listing parsed text. This is what makes "same card -> same group"
    reliable: two listings that matched the same catalog row always get the same
    key, even if their titles were worded differently or one was missing a set
    name. Falls back to the set_code on the parsed title only if the catalog
    lookup didn't carry set_key (shouldn't normally happen)."""
    set_part = (
        (matched_card.get("pokemon_sets") or {}).get("set_key")
        or parsed.set_code
        or "unknown_set"
    )

    if matched_card.get("promo_prefix") and matched_card.get("card_number"):
        try:
            num_str = str(int(matched_card["card_number"])).zfill(3)
        except (TypeError, ValueError):
            num_str = str(matched_card["card_number"])
        identity_part = f"{matched_card['promo_prefix'].lower()}{num_str}"
    elif matched_card.get("card_number") and matched_card.get("total_in_set"):
        identity_part = f"{matched_card['card_number']}_{matched_card['total_in_set']}"
    elif matched_card.get("card_number"):
        identity_part = str(matched_card["card_number"])
    else:
        identity_part = "unresolved"

    if parsed.grade_company and parsed.grade_value is not None:
        grade_num = int(parsed.grade_value) if float(parsed.grade_value).is_integer() else parsed.grade_value
        return f"charizard_{set_part}_{identity_part}_{parsed.grade_company.lower()}_{grade_num}"

    return f"charizard_{set_part}_{identity_part}_raw"


def normalize_charizard_key_from_parsed(parsed: ParsedTitle) -> str:
    if parsed.pokemon_name != "charizard":
        return "other_non_charizard"

    set_part = parsed.set_code or "unknown_set"

    if parsed.promo_code:
        identity_part = parsed.promo_code
    elif parsed.card_fraction_norm:
        left, _, right = parsed.card_fraction_norm.partition("/")
        identity_part = f"{left}_{right}"
    elif parsed.card_number and parsed.card_total_guess:
        identity_part = f"{parsed.card_number}_{parsed.card_total_guess}"
    elif parsed.card_number:
        identity_part = parsed.card_number
    else:
        identity_part = "unresolved"

    if parsed.grade_company and parsed.grade_value is not None:
        grade_num = int(parsed.grade_value) if float(parsed.grade_value).is_integer() else parsed.grade_value
        return f"charizard_{set_part}_{identity_part}_{parsed.grade_company.lower()}_{grade_num}"

    return f"charizard_{set_part}_{identity_part}_raw"


def extract_aspect_language(aspect_language: Optional[str], fallback: str) -> str:
    if not aspect_language:
        return fallback
    lowered = aspect_language.strip().lower()
    if lowered.startswith("japanese"):
        return "ja"
    if lowered.startswith("english"):
        return "en"
    return fallback


def map_item_to_bundle(item: dict, detail: Optional[dict] = None) -> dict:
    now_ts = datetime.now(timezone.utc).isoformat()
    title = item.get("title", "")
    aspect_data = extract_aspects(detail or item)
    parsed = parse_listing_title(title)

    if aspect_data.get("set_name"):
        detected_from_aspect = detect_set_from_text(normalize_text(aspect_data["set_name"]))
        if detected_from_aspect:
            parsed.set_code = detected_from_aspect
        elif not parsed.set_code:
            parsed.set_code = normalize_set_key(aspect_data["set_name"])

    if aspect_data.get("card_number_raw"):
        raw, num, total, fraction = extract_card_number_aspect(aspect_data["card_number_raw"])
        if raw:
            parsed.card_number_guess = raw
        if num:
            parsed.card_number = num
        if total:
            parsed.card_total_guess = total
        if fraction:
            parsed.card_fraction_norm = fraction

    parsed.language = extract_aspect_language(aspect_data.get("language"), parsed.language or "en")

    if aspect_data.get("grade_company") and not parsed.grade_company:
        parsed.grade_company = str(aspect_data["grade_company"]).strip().upper()

    if aspect_data.get("grade_value") is not None and parsed.grade_value is None:
        try:
            parsed.grade_value = float(aspect_data["grade_value"])
        except (TypeError, ValueError):
            pass

    if aspect_data.get("is_jumbo") or aspect_data.get("is_oversize"):
        parsed.is_junk = True
        parsed.junk_reason = parsed.junk_reason or "jumbo_or_oversize"

    if aspect_data.get("is_proxy") or aspect_data.get("is_custom"):
        parsed.is_junk = True
        parsed.junk_reason = parsed.junk_reason or "proxy_or_custom"

    if not parsed.set_code and aspect_data.get("set_name"):
        parsed.set_code = detect_set_from_text(normalize_text(aspect_data["set_name"])) or normalize_set_key(aspect_data["set_name"])

    resolved_set = get_or_create_set(parsed.set_code, aspect_data)
    if resolved_set and resolved_set.get("set_key"):
        parsed.set_code = resolved_set["set_key"]

    # Match against the catalog FIRST, then derive the group key from the match
    # itself when one exists. This is what keeps grouping locked to the catalog's
    # identity rather than to whatever the listing title happened to say -- two
    # listings for the same physical card always land in the same group, even if
    # one title omitted the set name or phrased it differently.
    matched_card = match_reference_card(parsed, aspect_data)
    if matched_card:
        normalized_item_key = normalize_charizard_key_from_match(matched_card, parsed)
    else:
        normalized_item_key = normalize_charizard_key_from_parsed(parsed)

    price = item.get("price", {}) or {}
    price_value = float(price.get("value")) if price.get("value") else None
    currency = price.get("currency", "USD")
    item_id = item.get("itemId")
    item_url = item.get("itemWebUrl")

    market_listing_row = {
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
    }

    sold_comp_row = {
        "id": str(uuid4()),
        "normalized_item_key": normalized_item_key,
        "source": "ebay_browse",
        "title": title,
        "sold_price_value": price_value,
        "sold_price_currency": currency,
        "shipping_value": None,
        "condition_text": item.get("condition"),
        "sold_at": now_ts,
        "item_web_url": item_url,
        "raw_json": item,
        "created_at": now_ts,
        "source_tier": 2,
        "source_run_id": "ebay_browse_charizard_rewrite",
        "external_comp_id": item_id,
        "search_query": "Charizard multi-query import",
        "sold_price": price_value,
        "shipping_price": None,
        "currency": currency,
        "comp_window_label": None,
        "grade_company": parsed.grade_company,
        "grade_value": parsed.grade_value,
        "listing_type": ",".join(item.get("buyingOptions", [])) if item.get("buyingOptions") else None,
        "confidence_grade": None,
        "is_valid_comp": not parsed.is_junk and parsed.pokemon_name == "charizard",
        "exclusion_reason": parsed.junk_reason if parsed.is_junk else None,
        "updated_at": now_ts,
    }

    listing_parse_row = {
        "parse_version": "v3_auto_set_resolution",
        "pokemon_name": parsed.pokemon_name,
        "set_code": parsed.set_code,
        "card_number_guess": parsed.card_number_guess,
        "card_number": parsed.card_number,
        "promo_code": parsed.promo_code,
        "variant": parsed.variant,
        "language": parsed.language,
        "grade_company": parsed.grade_company,
        "grade_value": parsed.grade_value,
        "is_junk": parsed.is_junk,
        "junk_reason": parsed.junk_reason,
        "match_confidence": 0.97 if matched_card else None,
        "matched_card_id": matched_card["id"] if matched_card else None,
        "normalized_item_key": normalized_item_key,
        "parser_notes": {
            "normalized_title": parsed.normalized_title,
            "card_total_guess": parsed.card_total_guess,
            "card_fraction_norm": parsed.card_fraction_norm,
            "aspect_data": aspect_data,
            "resolved_set_id": resolved_set["id"] if resolved_set else None,
            "resolved_set_name": resolved_set["set_name"] if resolved_set else None,
            "resolved_set_key": resolved_set["set_key"] if resolved_set else None,
            "final_set_code": parsed.set_code,
        },
    }

    return {
        "parsed": parsed,
        "market_listing_row": market_listing_row,
        "listing_parse_row": listing_parse_row,
        "sold_comp_row": sold_comp_row,
    }


def chunked(seq: List[dict], size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]



def load_parsed_csv_rows(csv_path: str) -> List[dict]:
    rows: List[dict] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def build_market_listing_id_map_from_parsed_source_ids(source_ids: List[str]) -> Dict[str, int]:
    listing_id_map: Dict[str, int] = {}
    unique_ids = sorted({sid for sid in source_ids if sid})
    for id_chunk in chunked(unique_ids, 200):
        result = (
            supabase.table("market_listings")
            .select("id,source_listing_id")
            .eq("source", "ebay")
            .in_("source_listing_id", id_chunk)
            .execute()
        )
        for row in result.data or []:
            listing_id_map[row["source_listing_id"]] = row["id"]
    return listing_id_map


def infer_grade_company_from_variant(variant: Optional[str]) -> Optional[str]:
    return None


def infer_grade_value_from_variant(variant: Optional[str]) -> Optional[float]:
    return None


def import_parsed_csv_to_listing_parses(csv_path: str) -> None:
    parsed_rows = load_parsed_csv_rows(csv_path)
    print(f"Loaded {len(parsed_rows)} parsed CSV rows from {csv_path}")

    source_ids = [row.get("source_record_id") for row in parsed_rows if row.get("source_record_id")]
    listing_id_map = build_market_listing_id_map_from_parsed_source_ids(source_ids)
    print(f"Matched {len(listing_id_map)} market_listings rows by source_listing_id")

    parse_rows: List[dict] = []
    skipped_missing_listing = 0

    for row in parsed_rows:
        source_record_id = row.get("source_record_id")
        market_listing_id = listing_id_map.get(source_record_id)

        if not market_listing_id:
            skipped_missing_listing += 1
            continue

        set_code = (row.get("set_slug") or row.get("set_name") or "").strip() or None
        card_number_guess = (row.get("card_number") or "").strip() or None
        promo_code = (row.get("promo_code") or "").strip() or None
        variant = (row.get("variant") or "").strip() or None
        language = (row.get("language") or "").strip() or None
        identity_status = (row.get("identity_status") or "").strip() or None
        identity_confidence_raw = (row.get("identity_confidence") or "").strip()

        card_number = None
        card_total_guess = None
        card_fraction_norm = None

        if card_number_guess:
            raw, num, total, fraction = extract_fraction_fields(card_number_guess)
            card_number_guess = raw or card_number_guess
            card_number = num
            card_total_guess = total
            card_fraction_norm = fraction

            if not card_number and not card_total_guess:
                compact = _split_compact_fraction_token(card_number_guess)
                if compact:
                    card_number, card_total_guess = compact
                    card_fraction_norm = f"{card_number}/{card_total_guess}"

        match_confidence = None
        if identity_confidence_raw:
            try:
                match_confidence = float(identity_confidence_raw)
            except ValueError:
                match_confidence = None

        parser_notes = {
            "import_source": "parsed-live-results-clean.csv",
            "canonical_name": row.get("canonical_name"),
            "currency": row.get("currency"),
            "price": row.get("price"),
            "condition_text": row.get("condition_text"),
            "identity_status": identity_status,
            "matched_fields": row.get("matched_fields"),
            "unresolved_fields": row.get("unresolved_fields"),
            "warnings": row.get("warnings"),
            "group_label": row.get("group_label"),
            "rule_pack": row.get("rule_pack"),
            "rule_version": row.get("rule_version"),
            "title": row.get("title"),
            "set_name": row.get("set_name"),
            "source_record_id": source_record_id,
        }

        parse_rows.append(
            {
                "market_listing_id": market_listing_id,
                "parse_version": "csv_import_v1",
                "pokemon_name": (row.get("canonical_name") or "").strip().lower() or "charizard",
                "set_code": set_code,
                "card_number_guess": card_number_guess,
                "card_number": card_number,
                "promo_code": promo_code,
                "variant": variant,
                "language": language,
                "grade_company": infer_grade_company_from_variant(variant),
                "grade_value": infer_grade_value_from_variant(variant),
                "is_junk": False,
                "junk_reason": None,
                "match_confidence": match_confidence,
                "matched_card_id": None,
                "normalized_item_key": None,
                "parser_notes": parser_notes,
            }
        )

    upsert_listing_parses_batch(parse_rows, chunk_size=100)
    print(f"Upserted {len(parse_rows)} listing_parses rows")
    print(f"Skipped {skipped_missing_listing} parsed rows with no matching market_listing")

def upsert_market_listings_batch(rows: List[dict], chunk_size: int = 100) -> Dict[str, int]:
    unique_rows = {row["source_listing_id"]: row for row in rows if row.get("source_listing_id")}
    deduped_rows = list(unique_rows.values())
    for row_chunk in chunked(deduped_rows, chunk_size):
        supabase.table("market_listings").upsert(row_chunk, on_conflict="source_listing_id").execute()
    listing_id_map: Dict[str, int] = {}
    source_ids = list(unique_rows.keys())
    for id_chunk in chunked(source_ids, 200):
        result = supabase.table("market_listings").select("id,source_listing_id").in_("source_listing_id", id_chunk).execute()
        for row in result.data or []:
            listing_id_map[row["source_listing_id"]] = row["id"]
    return listing_id_map


def upsert_listing_parses_batch(rows: List[dict], chunk_size: int = 100) -> None:
    for row_chunk in chunked(rows, chunk_size):
        supabase.table("listing_parses").upsert(row_chunk, on_conflict="market_listing_id").execute()


def insert_sold_comps(rows: List[dict], chunk_size: int = 100) -> int:
    external_ids = [row["external_comp_id"] for row in rows if row.get("external_comp_id")]
    existing_ids = set()
    for id_chunk in chunked(external_ids, 200):
        existing = (
            supabase.table("sold_comps")
            .select("external_comp_id")
            .eq("source", "ebay_browse")
            .in_("external_comp_id", id_chunk)
            .execute()
        )
        for row in existing.data or []:
            if row.get("external_comp_id"):
                existing_ids.add(row["external_comp_id"])
    new_rows = [row for row in rows if row.get("external_comp_id") not in existing_ids]
    inserted = 0
    for row_chunk in chunked(new_rows, chunk_size):
        supabase.table("sold_comps").insert(row_chunk).execute()
        inserted += len(row_chunk)
    return inserted


def run_charizard_enrichment_job(access_token: str, items: List[dict], chunk_size: int = 250, max_workers: int = 6, start_index: int = 0) -> None:
    target_items = items[start_index:]
    print(f"Starting enrichment job for {len(target_items)} items from start_index={start_index}")
    for chunk_index, item_chunk in enumerate(chunked(target_items, chunk_size), start=1):
        print(f"Starting chunk {chunk_index} with {len(item_chunk)} items")
        enriched_items = enrich_items_with_details_concurrent(access_token, item_chunk, max_workers=max_workers)
        bundles = [map_item_to_bundle(item, detail) for item, detail in enriched_items]
        market_rows = [b["market_listing_row"] for b in bundles]
        listing_id_map = upsert_market_listings_batch(market_rows, chunk_size=100)
        parse_rows = []
        for bundle in bundles:
            source_listing_id = bundle["market_listing_row"].get("source_listing_id")
            market_listing_id = listing_id_map.get(source_listing_id)
            if not market_listing_id:
                continue
            parse_row = dict(bundle["listing_parse_row"])
            parse_row["market_listing_id"] = market_listing_id
            parse_rows.append(parse_row)
        upsert_listing_parses_batch(parse_rows, chunk_size=100)
        print(f"Finished chunk {chunk_index}; sold comp insertion disabled")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--chunk-size", type=int, default=250)
    parser.add_argument("--max-workers", type=int, default=6)
    parser.add_argument("--max-items", type=int, default=1500)
    parser.add_argument(
        "--import-parsed-csv",
        type=str,
        default=None,
        help="Path to parsed CSV file to import into listing_parses",
    )
    args = parser.parse_args()

    if args.import_parsed_csv:
        import_parsed_csv_to_listing_parses(args.import_parsed_csv)
        print("Done.")
        return

    print("Getting eBay token...")
    token = get_ebay_access_token()
    items = get_charizard_items(token, max_items=args.max_items)
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