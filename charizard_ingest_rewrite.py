import argparse
import base64
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
SET_CODE_RE = re.compile(r"\b(sv\d{1,3}|svp|swsh\d*|swsh|sm\d*|sm|xy\d*|xy|bw\d+|bw|dp|pl|neo\d|base|ecard)\b", re.I)

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
    "star birth": "star_birth",
    "vmax climax": "vmax_climax",
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
    "17", "18", "20", "21", "25", "30", "32", "56", "68", "73", "78", "82", "83", "91",
    "94", "95", "97", "100", "101", "102", "105", "106", "107", "108", "110", "111",
    "112", "113", "122", "124", "130", "132", "135", "146", "147", "149", "159", "165",
    "172", "181", "185", "189", "197", "198", "199", "202", "203", "204", "211", "214",
    "215", "217", "230", "234", "236", "248", "307",
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
    set_guess: Optional[str]
    card_number_guess: Optional[str]
    card_number_norm: Optional[str]
    card_total_guess: Optional[str]
    card_fraction_norm: Optional[str]
    promo_code_guess: Optional[str]
    grade_company: Optional[str]
    grade_value: Optional[float]
    language_guess: Optional[str]
    variant_guess: Optional[str]
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


def _split_compact_fraction_token(token: str) -> Optional[Tuple[str, str]]:
    token = (token or "").strip()
    if not token.isdigit():
        return None

    if len(token) < 3 or len(token) > 6:
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
            return "prbw"

    for pattern in sorted(KNOWN_SET_NAME_PATTERNS, key=len, reverse=True):
        if pattern in t:
            normalized = SET_NAME_NORMALIZATIONS.get(pattern, slugify_set_name(pattern))
            override = SET_CANONICAL_OVERRIDES.get(normalized)
            if override:
                return override["set_key"]
            return normalized

    m = SET_CODE_RE.search(t)
    if m:
        return normalize_set_key(m.group(1))

    return None


def extract_language(t: str) -> str:
    if "japanese" in t or re.search(r"\bjp\b", t):
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


def parse_listing_title(title: str) -> ParsedTitle:
    t = normalize_text(title)
    is_junk, junk_reason = detect_junk(t)
    card_raw, card_norm, card_total, card_fraction = extract_fraction_fields(t)
    grade_company, grade_value = extract_grade(t)
    return ParsedTitle(
        raw_title=title,
        normalized_title=t,
        pokemon_name="charizard" if "charizard" in t else None,
        set_guess=detect_set_from_text(t),
        card_number_guess=card_raw,
        card_number_norm=card_norm,
        card_total_guess=card_total,
        card_fraction_norm=card_fraction,
        promo_code_guess=extract_promo_code(t),
        grade_company=grade_company,
        grade_value=grade_value,
        language_guess=extract_language(t),
        variant_guess=extract_variant(t),
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


def infer_set_insert_payload(set_guess: str, aspect_data: Optional[Dict[str, Optional[str]]] = None) -> dict:
    aspect_data = aspect_data or {}
    normalized_guess = normalize_set_key(set_guess) or "unknown_set"
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

    return {
        "set_key": set_key,
        "set_name": set_name,
        "series_name": None,
        "set_code": None,
        "language": "en",
        "release_date": None,
        "aliases": ",".join(aliases),
    }


def get_or_create_set(set_guess: Optional[str], aspect_data: Optional[Dict[str, Optional[str]]] = None) -> Optional[dict]:
    if not set_guess and not (aspect_data or {}).get("set_name"):
        return None

    aspect_data = aspect_data or {}
    candidate_inputs = [set_guess, aspect_data.get("set_name")]
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
        set_guess or normalize_set_key(aspect_data.get("set_name") or "unknown_set"),
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
    language = (parsed.language_guess or "en").lower()
    set_guess = parsed.set_guess
    card_num = parsed.card_number_norm
    card_total = parsed.card_total_guess
    promo_code = parsed.promo_code_guess

    if not set_guess and aspects.get("set_name"):
        set_guess = detect_set_from_text(normalize_text(aspects["set_name"])) or normalize_set_key(aspects["set_name"])

    if (not card_num or not card_total) and aspects.get("card_number_raw"):
        _, num, total, _ = extract_fraction_fields(aspects["card_number_raw"])
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

    if set_guess and card_num:
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


def normalize_charizard_key_from_parsed(parsed: ParsedTitle) -> str:
    if parsed.pokemon_name != "charizard":
        return "other_non_charizard"

    set_part = parsed.set_guess or "unknown_set"

    if parsed.promo_code_guess:
        identity_part = parsed.promo_code_guess
    elif parsed.card_fraction_norm:
        left, _, right = parsed.card_fraction_norm.partition("/")
        identity_part = f"{left}_{right}"
    elif parsed.card_number_norm and parsed.card_total_guess:
        identity_part = f"{parsed.card_number_norm}_{parsed.card_total_guess}"
    elif parsed.card_number_norm:
        identity_part = parsed.card_number_norm
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
            parsed.set_guess = detected_from_aspect
        elif not parsed.set_guess:
            parsed.set_guess = normalize_set_key(aspect_data["set_name"])

    if aspect_data.get("card_number_raw"):
        raw, num, total, fraction = extract_fraction_fields(aspect_data["card_number_raw"])
        if raw:
            parsed.card_number_guess = raw
        if num:
            parsed.card_number_norm = num
        if total:
            parsed.card_total_guess = total
        if fraction:
            parsed.card_fraction_norm = fraction

    parsed.language_guess = extract_aspect_language(aspect_data.get("language"), parsed.language_guess or "en")

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

    resolved_set = get_or_create_set(parsed.set_guess, aspect_data)
    if resolved_set and resolved_set.get("set_key"):
        parsed.set_guess = resolved_set["set_key"]

    normalized_item_key = normalize_charizard_key_from_parsed(parsed)

    price = item.get("price", {}) or {}
    price_value = float(price.get("value")) if price.get("value") else None
    currency = price.get("currency", "USD")
    item_id = item.get("itemId")
    item_url = item.get("itemWebUrl")
    matched_card = match_reference_card(parsed, aspect_data)

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
        "normalized_item_key": normalized_item_key,
        "parser_notes": {
            "normalized_title": parsed.normalized_title,
            "card_total_guess": parsed.card_total_guess,
            "card_fraction_norm": parsed.card_fraction_norm,
            "aspect_data": aspect_data,
            "resolved_set_id": resolved_set["id"] if resolved_set else None,
            "resolved_set_name": resolved_set["set_name"] if resolved_set else None,
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
        sold_rows = [b["sold_comp_row"] for b in bundles]
        inserted = insert_sold_comps(sold_rows, chunk_size=100)
        print(f"Finished chunk {chunk_index}; inserted {inserted} sold comps")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--chunk-size", type=int, default=250)
    parser.add_argument("--max-workers", type=int, default=6)
    parser.add_argument("--max-items", type=int, default=1500)
    args = parser.parse_args()

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