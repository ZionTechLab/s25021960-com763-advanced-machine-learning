"""
Offline parser tests — run with:  python scrapers/test_parsing.py

These use synthetic HTML fixtures in the three markup shapes the parser
supports. They verify the PARSING LOGIC without touching the network, so you
never burn live requests debugging selectors.

Important caveat: these fixtures are reconstructed from the rendered page,
not from riyasewana's real HTML source. Passing tests prove the label-driven
extraction works against plausible markup; they do not prove the real site
uses one of these shapes. Confirm that with:

    python scrapers/riyasewana.py probe --url <a real ad url>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from riyasewana import parse_detail  # noqa: E402
from schema import (  # noqa: E402
    flag_suspicious_mileage,
    guess_dealer,
    hash_seller,
    parse_mileage,
    parse_price,
)

PASSED = FAILED = 0


def check(label, got, want):
    global PASSED, FAILED
    if got == want:
        PASSED += 1
    else:
        FAILED += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


# --------------------------------------------------------------------------
# Fixtures — real values taken from live listings observed 2026-07-18
# --------------------------------------------------------------------------

TABLE_FIXTURE = """
<html><head>
<meta property="og:title" content="Toyota Premio" />
<meta property="og:description" content="Price: Rs. 14450000. 1st owner Mint condition" />
</head><body>
<h1>Toyota Premio 2017 Car (Used)</h1>
<p>Posted by Unity Lanka Enterprises · 2026-07-17 10:43 am, Kurunegala</p>
<table>
  <tr><td>Price</td><td>Rs. 14,450,000</td></tr>
  <tr><td>Location</td><td>Kurunegala</td><td>Year</td><td>2017</td></tr>
  <tr><td>Mileage</td><td>119 km</td><td>Make</td><td>Toyota</td></tr>
  <tr><td>Model</td><td>Premio</td><td>Gear</td><td>Automatic</td></tr>
  <tr><td>Fuel Type</td><td>Petrol</td><td>Engine (cc)</td><td>1500</td></tr>
  <tr><td>Condition</td><td>Registered (Used)</td><td>Ad Date</td><td>2026 Jul 17, 10:43 am</td></tr>
</table>
</body></html>
"""

NEGOTIABLE_FIXTURE = """
<html><head>
<meta property="og:title" content="Mazda Familia" />
<meta property="og:description" content="Negotiable price in Kandy" />
</head><body>
<h1>Mazda Familia 1990 Car</h1>
<table>
  <tr><td>Price</td><td>Negotiable</td></tr>
  <tr><td>Location</td><td>Kandy</td><td>Year</td><td>1990</td></tr>
  <tr><td>Mileage</td><td>180,000 km</td><td>Make</td><td>Mazda</td></tr>
  <tr><td>Model</td><td>Familia</td><td>Gear</td><td>Manual</td></tr>
  <tr><td>Fuel Type</td><td>Petrol</td><td>Engine (cc)</td><td>1300</td></tr>
  <tr><td>Condition</td><td>Registered (Used)</td></tr>
</table>
</body></html>
"""

DIV_FIXTURE = """
<html><head><meta property="og:title" content="Suzuki Alto A LTD" /></head><body>
<div>
  <span>Price</span><span>Rs. 6,550,000</span>
  <span>Location</span><span>Boralesgamuwa</span>
  <span>Year</span><span>2024</span>
  <span>Mileage</span><span>5,000 km</span>
  <span>Make</span><span>Suzuki</span>
  <span>Model</span><span>Alto A LTD</span>
  <span>Gear</span><span>Automatic</span>
  <span>Fuel Type</span><span>Petrol</span>
  <span>Engine (cc)</span><span>660</span>
  <span>Condition</span><span>Unregistered</span>
</div>
</body></html>
"""


# --------------------------------------------------------------------------

def test_price():
    print("parse_price")
    check("plain", parse_price("Rs. 14,450,000"), (14450000, False))
    check("no-dot", parse_price("Rs 5,650,000"), (5650000, False))
    check("negotiable", parse_price("Negotiable"), (None, True))
    check("empty", parse_price(""), (None, True))
    check("none", parse_price(None), (None, False))
    check("lakhs", parse_price("Rs. 56 lakhs"), (5600000, False))
    check("million", parse_price("Rs 1.2 million"), (1200000, False))


def test_mileage():
    print("parse_mileage")
    check("thousands", parse_mileage("113,000 km"), 113000)
    check("small", parse_mileage("119 km"), 119)
    check("empty", parse_mileage(""), None)
    check("bare", parse_mileage("242082"), 242082)


def test_mileage_flags():
    print("flag_suspicious_mileage")
    # 2017 Premio at 119 km — an actual observed listing. Nine years old.
    check("old car, tiny mileage",
          flag_suspicious_mileage(119, 2017, "Registered (Used)", 2026),
          "mileage_implausibly_low")
    # 1991 Sprinter at 1 km — also observed.
    check("very old, 1 km",
          flag_suspicious_mileage(1, 1991, "Registered (Used)", 2026),
          "mileage_implausibly_low")
    # Genuinely new vehicle: low mileage is expected, must NOT flag.
    check("unregistered 2025",
          flag_suspicious_mileage(1, 2025, "Unregistered", 2026), None)
    check("brand new",
          flag_suspicious_mileage(7, 2026, "Brand New", 2026), None)
    # Normal case
    check("normal",
          flag_suspicious_mileage(113000, 2018, "Registered (Used)", 2026), None)
    check("absurd high",
          flag_suspicious_mileage(5_000_000, 2010, "Registered (Used)", 2026),
          "mileage_implausibly_high")
    check("missing year", flag_suspicious_mileage(100, None, "", 2026), None)


def test_seller():
    print("seller handling")
    check("dealer", guess_dealer("Unity Lanka Enterprises"), True)
    check("dealer motors", guess_dealer("Perera Motors"), True)
    check("private", guess_dealer("Nimal"), False)
    check("none", guess_dealer(None), None)
    check("hash stable",
          hash_seller("Unity Lanka Enterprises"),
          hash_seller("unity lanka enterprises "))
    check("hash len", len(hash_seller("X")), 16)
    check("hash hides name", "unity" in hash_seller("Unity Lanka"), False)


def test_condition_is_canonical():
    """
    riyasewana condition strings must be mapped to the canonical vocabulary.

    This was missed initially: normalise_condition() was written and wired
    into ikman but never into riyasewana, so a pooled dataset carried both
    "Registered (Used)" and "used" as distinct categories. The mapping only
    has value if BOTH sources go through it.
    """
    print("condition mapped to canonical vocabulary")
    L = parse_detail(TABLE_FIXTURE, "https://riyasewana.com/buy/x-1", "car", 2026)
    check("riyasewana used", L.condition, "used")
    L2 = parse_detail(DIV_FIXTURE, "https://riyasewana.com/buy/y-2", "car", 2026)
    check("riyasewana unregistered", L2.condition, "reconditioned")
    check("no raw value leaks", "(" in L.condition, False)


def test_detail_table():
    print("parse_detail — table markup")
    L = parse_detail(TABLE_FIXTURE, "https://riyasewana.com/buy/toyota-premio-sale-kurunegala-11972104", "car", 2026)
    check("id", L.listing_id, "11972104")
    check("brand", L.brand, "Toyota")
    check("model", L.model, "Premio")
    check("year", L.year, 2017)
    check("price", L.price_lkr, 14450000)
    check("negotiable", L.is_negotiable, False)
    check("mileage", L.mileage_km, 119)
    check("fuel", L.fuel_type, "Petrol")
    check("gear", L.transmission, "Automatic")
    check("cc", L.engine_cc, 1500)
    check("condition normalised", L.condition, "used")
    check("location", L.location, "Kurunegala")
    check("body", L.body_type, "car")
    check("source", L.source_site, "riyasewana")
    check("dealer flagged", L.is_dealer_guess, True)
    check("seller not stored raw", "Unity" in str(L.seller_hash), False)
    check("mileage warned", "mileage_implausibly_low" in L.parse_warnings, True)


def test_detail_negotiable():
    print("parse_detail — negotiable listing")
    L = parse_detail(NEGOTIABLE_FIXTURE, "https://riyasewana.com/buy/mazda-familia-sale-kandy-11979444", "car", 2026)
    check("price none", L.price_lkr, None)
    check("negotiable true", L.is_negotiable, True)
    check("no false warning", "price_unparsed" in L.parse_warnings, False)
    check("year", L.year, 1990)
    check("mileage ok", L.mileage_km, 180000)


def test_detail_div():
    print("parse_detail — div/span markup fallback")
    L = parse_detail(DIV_FIXTURE, "https://riyasewana.com/buy/suzuki-alto-a-sale-boralesgamuwa-11979388", "car", 2026)
    check("brand", L.brand, "Suzuki")
    check("year", L.year, 2024)
    check("price", L.price_lkr, 6550000)
    check("cc", L.engine_cc, 660)
    check("condition normalised", L.condition, "reconditioned")
    # Unregistered vehicle with 5,000 km must not be flagged as implausible.
    check("no mileage warning", "mileage_implausibly_low" in L.parse_warnings, False)


TEXT_FIXTURE = """
<html><head>
<meta property="og:title" content="Toyota Premio" />
<meta property="og:description" content="Price: Rs. 14450000. 1st owner Mint condition" />
</head><body>
<p>Toyota Premio 2017 Car (Used)</p>
<p>Posted by Unity Lanka Enterprises · 2026-07-17 10:43 am, Kurunegala</p>
<p>2017 Petrol Automatic 119 km1500cc</p>
<p>LocationKurunegala Year2017 Mileage119 km MakeToyota ModelPremio
GearAutomatic Fuel TypePetrol Engine (cc)1500 ConditionRegistered (Used)
Ad Date2026 Jul 17, 10:43 am Options AIR CONDITION POWER STEERING</p>
</body></html>
"""


def test_detail_glued_text():
    """
    Labels glued to values, taken verbatim from the rendered text of a real
    listing (toyota-premio-...-11972104, observed 2026-07-18). No tables and
    no label/value element pairs, so this exercises the regex fallback —
    the only strategy testable against genuinely observed content.
    """
    print("parse_detail — glued label/value text (real observed content)")
    L = parse_detail(TEXT_FIXTURE, "https://riyasewana.com/buy/toyota-premio-sale-kurunegala-11972104", "car", 2026)
    check("brand", L.brand, "Toyota")
    check("model", L.model, "Premio")
    check("year", L.year, 2017)
    check("mileage", L.mileage_km, 119)
    check("gear", L.transmission, "Automatic")
    check("fuel", L.fuel_type, "Petrol")
    check("cc", L.engine_cc, 1500)
    check("price via meta", L.price_lkr, 14450000)
    check("dealer", L.is_dealer_guess, True)
    check("mileage warned", "mileage_implausibly_low" in L.parse_warnings, True)


REAL_MARKUP_FIXTURE = """
<html><head>
<meta property="og:title" content="Toyota Premio" />
<meta property="og:description" content="Price: Rs. 14450000. 1st owner" />
</head><body>
<div class="premium-badge">Promoted Ad</div>
<div class="vmore-title">
  <h1>Toyota Premio 2017 Car (Used)</h1>
  <div class="seller-info">Posted by Unity Lanka Enterprises &middot; 2026-07-17 10:43 am, Kurunegala</div>
</div>
<div class="price-card">
  <div class="price-section">
    <span class="price-label">Price</span>
    <div class="price-amount">Rs. 14,450,000</div>
  </div>
  <div class="price-section">
    <span class="price-label">Contact</span>
    <a class="call-btn ph-call" href="#">Show Phone</a>
  </div>
</div>
<div class="detail-row"><span class="detail-label">Location</span><span class="detail-value">Kurunegala</span></div>
<div class="detail-row"><span class="detail-label">Year</span><span class="detail-value">2017</span></div>
<div class="detail-row"><span class="detail-label">Mileage</span><span class="detail-value">119 km</span></div>
<div class="detail-row"><span class="detail-label">Make</span><span class="detail-value">Toyota</span></div>
<div class="detail-row"><span class="detail-label">Model</span><span class="detail-value">Premio</span></div>
<div class="detail-row"><span class="detail-label">Gear</span><span class="detail-value">Automatic</span></div>
<div class="detail-row"><span class="detail-label">Fuel Type</span><span class="detail-value">Petrol</span></div>
<div class="detail-row"><span class="detail-label">Engine (cc)</span><span class="detail-value">1500</span></div>
<div class="detail-row"><span class="detail-label">Condition</span><span class="detail-value">Registered (Used)</span></div>
<div class="detail-row"><span class="detail-label">Ad Date</span><span class="detail-value">2026 Jul 17, 10:43 am</span></div>
<div class="more-card">
  <div class="more-card-title">Options</div>
  <div class="options-list">
    <span class="option-chip">AIR CONDITION</span><span class="option-chip">POWER STEERING</span>
  </div>
</div>
<div class="more-card">
  <div class="more-card-title">More Details</div>
  <div class="more-card-body">1st owner<br />Mint condition</div>
</div>
<div class="views-count">355 views</div>
</body></html>
"""


def test_real_markup():
    """
    Regression test against riyasewana's actual markup, captured from a live
    page on 2026-07-18.

    The price-card is the important part. It holds two labels — "Price" and
    "Contact" — in sibling sections, and the price value is a <div>, not a
    <span>. An earlier version of the parser walked spans positionally and
    returned "Contact" as the price, producing a silently null target with
    is_negotiable wrongly set to True. On a full run that would have looked
    like a plausible negotiable rate rather than a bug.
    """
    print("parse_detail — real riyasewana markup (regression)")
    L = parse_detail(REAL_MARKUP_FIXTURE, "https://riyasewana.com/buy/toyota-premio-sale-kurunegala-11972104", "car", 2026)
    check("price parsed", L.price_lkr, 14450000)
    check("price raw", L.price_raw, "Rs. 14,450,000")
    check("not negotiable", L.is_negotiable, False)
    check("brand", L.brand, "Toyota")
    check("model", L.model, "Premio")
    check("year", L.year, 2017)
    check("mileage", L.mileage_km, 119)
    check("gear", L.transmission, "Automatic")
    check("fuel", L.fuel_type, "Petrol")
    check("cc", L.engine_cc, 1500)
    check("condition normalised", L.condition, "used")
    check("location", L.location, "Kurunegala")
    check("ad date", L.ad_date, "2026 Jul 17, 10:43 am")
    check("options", L.options, "AIR CONDITION|POWER STEERING")
    check("description", L.description, "1st owner Mint condition")
    check("promoted", L.is_promoted, True)
    check("views", L.views, 355)
    check("dealer", L.is_dealer_guess, True)
    check("seller hashed", L.seller_hash, hash_seller("Unity Lanka Enterprises"))
    check("mileage flagged", "mileage_implausibly_low" in L.parse_warnings, True)


if __name__ == "__main__":
    for fn in (
        test_price, test_mileage, test_mileage_flags, test_seller,
        test_condition_is_canonical,
        test_detail_table, test_detail_negotiable, test_detail_div,
        test_detail_glued_text, test_real_markup,
    ):
        fn()
    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)
