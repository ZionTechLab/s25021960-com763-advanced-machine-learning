"""
Offline tests for the ikman scraper — python scrapers/test_ikman.py

Covers the robots.txt compliance gate and the detail parser. The fixture
below is reconstructed from a live listing observed 2026-07-18; confirm the
real markup with `probe` before a long run.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ikman import is_blocked, parse_detail  # noqa: E402
from schema import normalise_body_type, normalise_condition  # noqa: E402

PASSED = FAILED = 0


def check(label, got, want):
    global PASSED, FAILED
    if got == want:
        PASSED += 1
    else:
        FAILED += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


def test_robots_gate():
    print("is_blocked — robots.txt compliance")
    ok = "https://ikman.lk/en/ad/nissan-x-trail-t32-hybrid-2wd-2015-for-sale-colombo"
    check("plain ad allowed", is_blocked(ok), False)
    check("category allowed", is_blocked("https://ikman.lk/en/ads/sri-lanka/cars"), False)
    check("pagination allowed", is_blocked("https://ikman.lk/en/ads/sri-lanka/cars?page=3"), False)
    # Disallowed patterns
    check("double hyphen", is_blocked("https://ikman.lk/en/ad/foo--bar"), True)
    check("filters param", is_blocked("https://ikman.lk/en/ads/sri-lanka/cars?filters=x"), True)
    check("sort param", is_blocked("https://ikman.lk/en/ads/sri-lanka/cars?sort=date"), True)
    check("type param", is_blocked("https://ikman.lk/en/ads/sri-lanka/cars?type=x"), True)
    check("query param", is_blocked("https://ikman.lk/en/ads/sri-lanka/cars?query=toyota"), True)
    check("tree.brand", is_blocked("https://ikman.lk/en/ads/x?tree.brand=toyota"), True)
    check("utm_source", is_blocked("https://ikman.lk/en/ad/x?utm_source=fb"), True)
    check("login-modal", is_blocked("https://ikman.lk/en/ad/x?login-modal=true"), True)
    check("ad delete", is_blocked("https://ikman.lk/en/ad/x/delete"), True)
    check("ad edit", is_blocked("https://ikman.lk/en/ad/x/edit"), True)
    check("ad report", is_blocked("https://ikman.lk/en/ad/x/report"), True)
    check("promote", is_blocked("https://ikman.lk/en/ad/x/promote"), True)
    check("saved-search", is_blocked("https://ikman.lk/en/saved-search/12"), True)
    check("ads-filters", is_blocked("https://ikman.lk/en/ads-filters"), True)


def test_vocab_mapping():
    print("cross-site vocabulary mapping")
    check("riya used", normalise_condition("Registered (Used)"), "used")
    check("ikman used", normalise_condition("Used"), "used")
    check("riya recon", normalise_condition("Unregistered (Recondition)"), "reconditioned")
    check("ikman recon", normalise_condition("Reconditioned"), "reconditioned")
    check("riya new", normalise_condition("Brand New"), "new")
    check("ikman new", normalise_condition("New"), "new")
    check("antique", normalise_condition("Antique"), "antique")
    # Body type: the site-asymmetry this exists to resolve
    check("ikman suv", normalise_body_type("SUV / 4x4"), "suv")
    check("hatchback is car", normalise_body_type("Hatchback"), "car")
    check("saloon is car", normalise_body_type("Saloon"), "car")
    check("mpv is van", normalise_body_type("MPV"), "van")


FIXTURE = """
<html><head>
<meta property="og:title" content="Nissan X-Trail T32 Hybrid 2WD 2015 for Sale in Polgasowita | ikman" />
<meta property="og:description" content="Nissan X-Trail T32 Hybrid 2015 - Well Maintained Family SUV" />
</head><body>
<h1>Nissan X-Trail T32 Hybrid 2WD 2015</h1>
<div>Posted on 04 Jul 11:03 am, Polgasowita, Colombo</div>
<div>3108 views</div>
<div>Rs 10,200,000</div>
<div>Negotiable</div>
<div class="r"><div>Brand:</div><div>Nissan</div></div>
<div class="r"><div>Model:</div><div>X-Trail</div></div>
<div class="r"><div>Trim / Edition:</div><div>T32 Hybrid 2WD</div></div>
<div class="r"><div>Year of Manufacture:</div><div>2015</div></div>
<div class="r"><div>Condition:</div><div>Used</div></div>
<div class="r"><div>Transmission:</div><div>Automatic</div></div>
<div class="r"><div>Body type:</div><div>SUV / 4x4</div></div>
<div class="r"><div>Fuel type:</div><div>Petrol</div></div>
<div class="r"><div>Engine capacity:</div><div>2,000 cc</div></div>
<div class="r"><div>Mileage:</div><div>109,010 km</div></div>
<div>For sale by Pasindu Dewapriya 0718XXXXXX Click to show phone number</div>
</body></html>
"""

URL = "https://ikman.lk/en/ad/nissan-x-trail-t32-hybrid-2wd-2015-for-sale-colombo"


def test_detail():
    print("parse_detail — ikman markup")
    L = parse_detail(FIXTURE, URL, "", 2026)
    check("listing_id", L.listing_id, "nissan-x-trail-t32-hybrid-2wd-2015-for-sale-colombo")
    check("source", L.source_site, "ikman")
    check("brand", L.brand, "Nissan")
    check("model", L.model, "X-Trail")
    check("trim", L.trim, "T32 Hybrid 2WD")
    check("year", L.year, 2015)
    check("transmission", L.transmission, "Automatic")
    check("fuel", L.fuel_type, "Petrol")
    check("engine cc", L.engine_cc, 2000)
    check("mileage", L.mileage_km, 109010)
    check("condition normalised", L.condition, "used")
    check("body type normalised", L.body_type, "suv")
    check("views", L.views, 3108)
    check("no warnings", L.parse_warnings, [])


def test_negotiable_semantics():
    """
    The trap this guards against.

    On riyasewana a listing shows EITHER a price OR "Negotiable", so
    is_negotiable implies price_lkr is None. On ikman the ad shows a price
    AND may additionally be tagged Negotiable. Reusing riyasewana's logic
    here would null out the target on most of ikman's listings — a silent
    loss that looks like a high missing-price rate rather than a bug.
    """
    print("parse_detail — negotiable does NOT null the price on ikman")
    L = parse_detail(FIXTURE, URL, "", 2026)
    check("price kept", L.price_lkr, 10200000)
    check("negotiable flagged", L.is_negotiable, True)
    check("both true together", (L.price_lkr is not None) and L.is_negotiable, True)
    check("no price warning", "price_unparsed" in L.parse_warnings, False)


def test_location_and_description():
    """Location has no label on ikman; it trails the posting date."""
    print("parse_detail — location and description")
    L = parse_detail(FIXTURE, URL, "", 2026)
    check("location", L.location, "Polgasowita, Colombo")
    check("no stray space", " ," in L.location, False)
    check("no missing_location warning", "missing_location" in L.parse_warnings, False)


VAN_FIXTURE = """
<html><head>
<meta property="og:title" content="Suzuki Every FULL JOIN NON TURBO 2026 | Kohuwala | ikman" />
</head><body>
<h1>Suzuki Every FULL JOIN NON TURBO 2026</h1>
<div>Posted on 15 Jul 09:20 am, Kohuwala, Colombo</div>
<div>412 views</div>
<div>Rs 8,950,000</div>
<div class="r"><div>Brand:</div><div>Suzuki</div></div>
<div class="r"><div>Model:</div><div>Every</div></div>
<div class="r"><div>Trim / Edition:</div><div>FULL JOIN NON TURBO</div></div>
<div class="r"><div>Condition:</div><div>New</div></div>
<div class="r"><div>Model year:</div><div>2026</div></div>
<div class="r"><div>Mileage:</div><div>0 km</div></div>
<div class="r"><div>Engine capacity:</div><div>660 cc</div></div>
</body></html>
"""


def test_van_schema():
    """
    Van listings use a REDUCED and differently-labelled field set.

    "Model year" instead of "Year of Manufacture" — missing that label left
    763 of 3,724 rows (20%) without a year on the first real export, and the
    warning looked like sparse source data rather than a mapping gap.

    Vans also genuinely carry no Transmission or Fuel type field. That is
    real absence, not a parse failure, and Phase 3 must handle it as such.
    """
    print("parse_detail — van schema (Model year, no transmission/fuel)")
    L = parse_detail(VAN_FIXTURE, "https://ikman.lk/en/ad/suzuki-every-2026-for-sale-colombo-3", "van", 2026)
    check("year via 'Model year'", L.year, 2026)
    check("no missing_year warning", "missing_year" in L.parse_warnings, False)
    check("brand", L.brand, "Suzuki")
    check("model", L.model, "Every")
    check("trim", L.trim, "FULL JOIN NON TURBO")
    check("condition", L.condition, "new")
    check("engine cc", L.engine_cc, 660)
    check("price", L.price_lkr, 8950000)
    check("body type from hint", L.body_type, "van")
    # Genuinely absent on vans — must stay empty, not be invented
    check("transmission absent", L.transmission, "")
    check("fuel absent", L.fuel_type, "")
    # New-vehicle 0 km must not be flagged implausible
    check("0 km on new not flagged", "mileage_implausibly_low" in L.parse_warnings, False)


def test_body_type_hint_fallback():
    print("body_type falls back to category hint when field absent")
    stripped = FIXTURE.replace("<div class=\"r\"><div>Body type:</div><div>SUV / 4x4</div></div>", "")
    L = parse_detail(stripped, URL, "van", 2026)
    check("hint used", L.body_type, "van")


if __name__ == "__main__":
    for fn in (test_robots_gate, test_vocab_mapping, test_detail,
               test_negotiable_semantics, test_location_and_description,
               test_van_schema, test_body_type_hint_fallback):
        fn()
    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)
