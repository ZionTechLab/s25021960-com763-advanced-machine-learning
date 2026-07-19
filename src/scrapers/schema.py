"""
Canonical schema and shared parsing helpers.

Both site scrapers (riyasewana, ikman) emit rows in this shape so the two
datasets can be concatenated in Phase 3 without reconciliation guesswork.

Design rule: this module PARSES, it does not CLEAN. Implausible values are
preserved as-is and flagged, never silently corrected. Cleaning decisions
belong in Phase 3 where they can be documented and justified in the report.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from typing import Optional

# --------------------------------------------------------------------------
# Canonical schema
# --------------------------------------------------------------------------

CANONICAL_FIELDS = [
    "listing_id",       # site-local ad id
    "source_site",      # 'riyasewana' | 'ikman'
    "url",
    "scrape_timestamp",  # ISO8601, when WE fetched it
    "ad_date",          # when the ad was posted, as given by the site
    "title",
    "brand",
    "model",
    "trim",             # ikman separates trim/edition; riyasewana folds it
                        # into `model`. Phase 3 should concatenate model+trim
                        # for ikman rows before comparing across sites.
    "year",
    "price_lkr",        # None when the ad says "Negotiable"
    "price_raw",        # original string, kept for audit
    "is_negotiable",
    "mileage_km",
    "mileage_raw",
    "fuel_type",
    "transmission",
    "engine_cc",
    "body_type",        # car | van | suv
    "condition",        # Registered / Unregistered / Brand New / Antique
    "location",
    "options",          # pipe-separated extras (A/C, power steering, ...)
    "description",
    "seller_hash",      # sha256[:16] of seller name — NOT the name itself
    "is_dealer_guess",  # heuristic, see guess_dealer()
    "is_promoted",      # paid placement; likely dealer stock, keep as a control
    "views",            # ad view count, a rough proxy for time listed
    "parse_warnings",   # pipe-separated flags for Phase 3 to act on
]


@dataclass
class Listing:
    listing_id: str = ""
    source_site: str = ""
    url: str = ""
    scrape_timestamp: str = ""
    ad_date: str = ""
    title: str = ""
    brand: str = ""
    model: str = ""
    trim: str = ""
    year: Optional[int] = None
    price_lkr: Optional[int] = None
    price_raw: str = ""
    is_negotiable: bool = False
    mileage_km: Optional[int] = None
    mileage_raw: str = ""
    fuel_type: str = ""
    transmission: str = ""
    engine_cc: Optional[int] = None
    body_type: str = ""
    condition: str = ""
    location: str = ""
    options: str = ""
    description: str = ""
    seller_hash: str = ""
    is_dealer_guess: Optional[bool] = None
    is_promoted: Optional[bool] = None
    views: Optional[int] = None
    parse_warnings: list = field(default_factory=list)

    def to_row(self) -> dict:
        d = asdict(self)
        d["parse_warnings"] = "|".join(self.parse_warnings)
        return {k: d[k] for k in CANONICAL_FIELDS}


# --------------------------------------------------------------------------
# Price
# --------------------------------------------------------------------------

_NEGOTIABLE_TOKENS = {"negotiable", "neg", "price on request", "call", "-", ""}


def parse_price(raw: Optional[str]) -> tuple[Optional[int], bool]:
    """
    Return (price_lkr, is_negotiable).

    Roughly a quarter of riyasewana listings show "Negotiable" instead of a
    figure. Those rows have no target variable and must be dropped in Phase 3
    — but they are recorded here, because whether they are missing at random
    is itself a question the report should answer rather than assume.

    >>> parse_price("Rs. 14,450,000")
    (14450000, False)
    >>> parse_price("Negotiable")
    (None, True)
    >>> parse_price("Rs. 56 lakhs")
    (5600000, False)
    """
    if raw is None:
        return None, False
    s = raw.strip()
    if s.lower().strip(". ") in _NEGOTIABLE_TOKENS:
        return None, True

    low = s.lower()

    # "56 lakhs" / "56 lakh" -> 5,600,000
    m = re.search(r"([\d,.]+)\s*lakh", low)
    if m:
        try:
            return int(round(float(m.group(1).replace(",", "")) * 100_000)), False
        except ValueError:
            return None, True

    # "1.2 million"
    m = re.search(r"([\d,.]+)\s*(?:million|mn)\b", low)
    if m:
        try:
            return int(round(float(m.group(1).replace(",", "")) * 1_000_000)), False
        except ValueError:
            return None, True

    digits = re.sub(r"[^\d]", "", s)
    if not digits:
        return None, True
    return int(digits), False


# --------------------------------------------------------------------------
# Mileage
# --------------------------------------------------------------------------

def parse_mileage(raw: Optional[str]) -> Optional[int]:
    """
    Parse the numeric mileage. Deliberately does NOT correct suspicious values.

    >>> parse_mileage("113,000 km")
    113000
    >>> parse_mileage("119 km")
    119
    >>> parse_mileage("")
    """
    if not raw:
        return None
    digits = re.sub(r"[^\d]", "", raw.split("km")[0] if "km" in raw.lower() else raw)
    return int(digits) if digits else None


def flag_suspicious_mileage(
    mileage_km: Optional[int],
    year: Optional[int],
    condition: str,
    current_year: int,
) -> Optional[str]:
    """
    Flag — not fix — mileage that is implausible for the vehicle's age.

    Observed on riyasewana: a 2017 Toyota Premio listed at "119 km", a 1991
    Sprinter at "1 km", a 1994 Familia at "111 km". These are almost certainly
    sellers entering mileage in THOUSANDS. Treating them as literal produces
    training rows saying a nine-year-old car has done 119 km, which will drag
    the mileage coefficient badly.

    Brand-new and unregistered vehicles legitimately show very low mileage,
    so condition is taken into account before flagging.

    Returns a warning string, or None if the value looks plausible.
    """
    if mileage_km is None or year is None:
        return None

    age = max(current_year - year, 0)
    cond = (condition or "").lower()
    genuinely_new = ("brand new" in cond) or ("unregistered" in cond) or age <= 1

    if genuinely_new:
        return None
    if mileage_km < 1000 and age >= 3:
        return "mileage_implausibly_low"
    if mileage_km > 1_000_000:
        return "mileage_implausibly_high"
    return None


# --------------------------------------------------------------------------
# Other fields
# --------------------------------------------------------------------------

def parse_int(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    digits = re.sub(r"[^\d]", "", raw)
    return int(digits) if digits else None


def parse_year(raw: Optional[str]) -> Optional[int]:
    y = parse_int(raw)
    if y is None:
        return None
    return y if 1900 <= y <= 2100 else None


_DEALER_MARKERS = (
    "enterprise", "motors", "motor", "auto", "traders", "trading", "cars",
    "lanka", "pvt", "ltd", "company", "agency", "agencies", "centre",
    "center", "sales", "holdings", "group", "garage", "showroom", "leasing",
)


def guess_dealer(seller_name: Optional[str]) -> Optional[bool]:
    """
    Heuristic: does the seller name look like a business?

    Dealers price differently from private sellers, so this is a genuine
    feature. It is a guess and is named accordingly — validate the hit rate
    on a manual sample in Phase 3 before trusting it as a predictor.
    """
    if not seller_name:
        return None
    low = seller_name.lower()
    return any(marker in low for marker in _DEALER_MARKERS)


def hash_seller(seller_name: Optional[str]) -> str:
    """
    Store a hash, never the name.

    The seller identity is needed for deduplication (a dealer relisting the
    same stock) but the raw name is personal data for private sellers and is
    not required for any modelling purpose. Hashing preserves the dedup
    signal while keeping personal data out of the dataset.
    """
    if not seller_name:
        return ""
    return hashlib.sha256(seller_name.strip().lower().encode("utf-8")).hexdigest()[:16]


def normalise_ws(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", text).strip() if text else ""


# --------------------------------------------------------------------------
# Cache size control
# --------------------------------------------------------------------------

_BOILERPLATE_RE = re.compile(r"<(script|style|svg|noscript)\b[^>]*>.*?</\1>",
                             re.S | re.I)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def strip_boilerplate(html: Optional[str]) -> str:
    """
    Remove script, style, svg, noscript and comment content from a page.

    ikman is a React application that embeds a large JSON state blob and
    inline SVG icons in every page: a listing arrives at ~840 KB, of which
    roughly 95% is markup we never read. Caching pages whole produced a
    3.4 GB SQLite file for ~3,700 listings and made export slow enough to
    fail. Stripping first yields ~45 KB per page.

    Verified on sampled live pages: parse_detail() output is identical
    before and after stripping, because every field the parser reads lives
    in ordinary server-rendered HTML rather than in the script payload.

    Do not extend this to tags that might carry data. If a site ever moves
    listing content into a <script type="application/ld+json"> block, that
    tag must be preserved here first.
    """
    if not html:
        return ""
    return _COMMENT_RE.sub("", _BOILERPLATE_RE.sub("", html))


# --------------------------------------------------------------------------
# Cross-site vocabulary reconciliation
# --------------------------------------------------------------------------

# The two sites use different condition vocabularies for the same underlying
# states. Mapping them here, at collection time, means Phase 3 never has to
# guess whether "Registered (Used)" and "Used" are the same thing.
#
#   riyasewana                   ikman           canonical
#   Registered (Used)            Used            used
#   Unregistered (Recondition)   Reconditioned   reconditioned
#   Brand New                    New             new
#   Antique                      —               antique
_CONDITION_MAP = {
    "registered (used)": "used",
    "registered": "used",
    "used": "used",
    "unregistered (recondition)": "reconditioned",
    "unregistered": "reconditioned",
    "recondition": "reconditioned",
    "reconditioned": "reconditioned",
    "brand new": "new",
    "new": "new",
    "antique": "antique",
    # ikman-only value, 96 rows in the 2026-07-18 pull. Kept distinct rather
    # than folded into "reconditioned": both describe imported stock, but we
    # have not confirmed ikman uses them interchangeably, and collapsing two
    # categories on an assumption is not reversible later.
    "import": "import",
}


def normalise_condition(raw: Optional[str]) -> str:
    """Map a site-specific condition string to the canonical vocabulary."""
    if not raw:
        return ""
    return _CONDITION_MAP.get(normalise_ws(raw).lower(), normalise_ws(raw).lower())


# ikman exposes body type as a per-listing field ("SUV / 4x4", "Hatchback").
# riyasewana only implies it via the search category the ad was found under.
_BODY_TYPE_MAP = {
    "suv / 4x4": "suv",
    "suv": "suv",
    "jeep": "suv",
    "hatchback": "car",
    "saloon": "car",
    "sedan": "car",
    "station wagon": "car",
    "coupe / sports": "car",
    "coupé/sports": "car",
    "coupe/sports": "car",
    "coupe": "car",
    "convertible": "car",
    "mpv": "van",
    "van": "van",
    "mini van": "van",
    "car": "car",
}


def normalise_body_type(raw: Optional[str]) -> str:
    """
    Collapse body-type strings to car / suv / van.

    Note the asymmetry this resolves: riyasewana treats SUVs as a separate
    top-level category, while ikman files them under `cars` and records the
    body type as a field. Without this mapping the same vehicle would carry
    different body_type values depending on which site it came from.
    """
    if not raw:
        return ""
    return _BODY_TYPE_MAP.get(normalise_ws(raw).lower(), normalise_ws(raw).lower())
