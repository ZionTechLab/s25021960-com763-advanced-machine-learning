"""
ikman.lk scraper — Phase 2, second source.

    python scrapers/ikman.py probe --url https://ikman.lk/en/ad/<some-ad>
    python scrapers/ikman.py harvest --categories cars vans --max-pages 120
    python scrapers/ikman.py fetch --limit 2000 --max-cooldowns 6
    python scrapers/ikman.py export --out data/raw/ikman.csv

Compliance (robots.txt retrieved 2026-07-18):
  ikman disallows URLs carrying search/filter/sort/query parameters, and any
  URL containing a double hyphen. Plain category pagination (?page=N) is not
  in the disallow list and is used here; `is_blocked()` enforces every rule
  below and is the single gate all fetches pass through.

  robots.txt also publishes sitemap indices. `harvest --source sitemap` uses
  sitemap-listings-vehicles-{1,2,3}.xml.gz as an alternative to pagination.
  Note the sitemaps lagged ~9 days behind live listings when checked, so
  pagination is the default and the sitemap is the deeper-but-staler option.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import io
import random
import re
import sqlite3
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))
from schema import (  # noqa: E402
    CANONICAL_FIELDS,
    Listing,
    flag_suspicious_mileage,
    guess_dealer,
    hash_seller,
    normalise_body_type,
    normalise_condition,
    normalise_ws,
    parse_int,
    parse_mileage,
    parse_price,
    parse_year,
    strip_boilerplate,
)

BASE = "https://ikman.lk"
SOURCE = "ikman"

DEFAULT_DELAY = 4.0
MAX_CONSECUTIVE_429 = 5
# Never sleep longer than this inside a run. A Retry-After above it means
# the site has hard-limited us; report and exit rather than hang for hours.
MAX_INLINE_SLEEP = 900
COOLDOWN_SECONDS = 1800

DB_PATH = Path("data/raw/ikman_cache.sqlite")

USER_AGENT = (
    "MScResearchBot/1.0 (postgraduate coursework; vehicle price research; "
    "contact: t.perera@hayleysadvantis.com)"
)

CATEGORY_PATHS = {
    "cars": "/en/ads/sri-lanka/cars",
    "vans": "/en/ads/sri-lanka/vans",
}

SITEMAPS = [
    f"{BASE}/sitemap-listings-vehicles-{i}.xml.gz" for i in (1, 2, 3)
]

# Query parameters robots.txt disallows anywhere in the URL.
BLOCKED_PARAMS = (
    "utm_source", "c=", "account=", "filters=", "sort=", "type=", "query=",
    "email=", "tree.brand=", "someParam=", "phones=", "gclid=", "scope=",
    "locale=", "similar=", "categoryName=", "categoryType=", "short=",
    "actions=", "deprecation_warning=", "switch_locale=", "shop_required=",
    "nearby=", "login-modal",
)

BLOCKED_SUBSTRINGS = (
    "--",                    # robots: Disallow: /*--*
    "/ads-locations", "/ads-categories", "/ads-filters", "/ads-type",
    "/saved-search/", "/134461134/",
    "/password-reset", "/password-update",
    "/confirm", "/promote", "/payment/transaction", "/select-payment",
)

AD_PATH_RE = re.compile(r"^/en/ad/[\w\-/]+$")


def is_blocked(url: str) -> bool:
    """
    Single compliance gate. Returns True if robots.txt disallows this URL.

    Deliberately conservative: when a rule is ambiguous, treat the URL as
    blocked. Losing a few listings costs far less than ignoring a stated
    crawl preference.
    """
    parsed = urllib.parse.urlparse(url)
    path_and_query = parsed.path + ("?" + parsed.query if parsed.query else "")

    if any(sub in path_and_query for sub in BLOCKED_SUBSTRINGS):
        return True
    if parsed.query and any(p in parsed.query for p in BLOCKED_PARAMS):
        return True
    if re.search(r"/ad/[^/]+/(delete|edit|report)", parsed.path):
        return True
    return False


# --------------------------------------------------------------------------
# Cache + fetch (mirrors riyasewana.py; kept separate so the two scrapers can
# run concurrently against independent databases)
# --------------------------------------------------------------------------

def db_connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS pages (
            url TEXT PRIMARY KEY, html TEXT, status INTEGER, fetched_at TEXT
        );
        CREATE TABLE IF NOT EXISTS listing_urls (
            url TEXT PRIMARY KEY, body_type TEXT, discovered_at TEXT
        );
        """
    )
    conn.commit()
    return conn


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en"})
    return s


def fetch(conn, url, session, delay=DEFAULT_DELAY, force=False, binary=False):
    """Fetch with caching. Returns (content, status, retry_after)."""
    if is_blocked(url):
        raise ValueError(f"Refusing to fetch robots.txt-disallowed URL: {url}")

    if not force and not binary:
        row = conn.execute("SELECT html, status FROM pages WHERE url=?", (url,)).fetchone()
        if row and row[1] == 200:
            return row[0], row[1], None

    time.sleep(delay + random.uniform(0, delay * 0.5))

    retry_after = None
    try:
        resp = session.get(url, timeout=30)
        status = resp.status_code
        # ikman serves "Content-Type: text/html" with no charset, so requests
        # falls back to ISO-8859-1 per RFC 2616 and mangles every non-ASCII
        # character — em-dashes and emoji in descriptions arrive as "â".
        # Sinhala and Tamil listing text would be destroyed outright.
        if not binary and "charset" not in resp.headers.get("Content-Type", "").lower():
            resp.encoding = resp.apparent_encoding or "utf-8"
        content = resp.content if binary else resp.text
        if status == 429:
            content = None
            ra = resp.headers.get("Retry-After")
            if ra and ra.isdigit():
                retry_after = int(ra)
    except requests.RequestException as exc:
        print(f"  ! request failed {url}: {exc}", file=sys.stderr)
        content, status = None, 0

    if not binary:
        # Strip before storing: ikman pages are ~840 KB, ~95% of which is
        # script/style/svg we never parse. See schema.strip_boilerplate.
        conn.execute(
            "INSERT OR REPLACE INTO pages (url, html, status, fetched_at) VALUES (?,?,?,?)",
            (url, strip_boilerplate(content) if content else content, status,
             dt.datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
    return content, status, retry_after


# --------------------------------------------------------------------------
# Harvest
# --------------------------------------------------------------------------

def _record_urls(conn, urls, body_type) -> int:
    new = 0
    for u in urls:
        cur = conn.execute(
            "INSERT OR IGNORE INTO listing_urls (url, body_type, discovered_at) VALUES (?,?,?)",
            (u, body_type, dt.datetime.now().isoformat(timespec="seconds")),
        )
        new += cur.rowcount
    conn.commit()
    return new


def harvest_pagination(categories, max_pages, delay) -> None:
    conn, session = db_connect(), make_session()
    total = 0
    for cat in categories:
        path = CATEGORY_PATHS.get(cat)
        if not path:
            print(f"  unknown category {cat}, skipping")
            continue
        print(f"\n== harvesting {cat} ==")
        for page in range(1, max_pages + 1):
            url = f"{BASE}{path}" + (f"?page={page}" if page > 1 else "")
            html, status, _ = fetch(conn, url, session, delay)
            if not html or status != 200:
                print(f"  page {page}: status {status}, stopping")
                break
            soup = BeautifulSoup(html, "html.parser")
            found = set()
            for a in soup.find_all("a", href=True):
                full = urllib.parse.urljoin(BASE, a["href"]).split("?")[0]
                p = urllib.parse.urlparse(full)
                if p.netloc.endswith("ikman.lk") and AD_PATH_RE.match(p.path) and not is_blocked(full):
                    found.add(full)
            if not found:
                print(f"  page {page}: no ad links — stopping")
                break
            new = _record_urls(conn, found, cat.rstrip("s"))
            total += new
            print(f"  page {page}: {len(found)} links, {new} new")
    n = conn.execute("SELECT COUNT(*) FROM listing_urls").fetchone()[0]
    print(f"\nharvest complete: {total} new, {n} total")


def harvest_sitemap(delay) -> None:
    """
    Alternative harvest via the sitemaps robots.txt publishes.

    Deeper than pagination but staler — lastmod was ~9 days behind when
    checked, so expect some listings to be sold or removed by fetch time.
    """
    conn, session = db_connect(), make_session()
    total = 0
    for sm in SITEMAPS:
        print(f"\n== {sm} ==")
        blob, status, _ = fetch(conn, sm, session, delay, binary=True)
        if not blob or status != 200:
            print(f"  status {status}, skipping")
            continue
        try:
            xml = gzip.GzipFile(fileobj=io.BytesIO(blob)).read()
        except OSError:
            xml = blob  # served uncompressed
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            print(f"  XML parse failed: {exc}")
            continue
        ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        urls = set()
        for loc in root.findall(".//s:loc", ns):
            u = (loc.text or "").strip()
            p = urllib.parse.urlparse(u)
            if AD_PATH_RE.match(p.path) and not is_blocked(u):
                urls.add(u)
        new = _record_urls(conn, urls, "")
        total += new
        print(f"  {len(urls)} vehicle ad URLs, {new} new")
    print(f"\nsitemap harvest complete: {total} new")


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

DETAIL_LABELS = {
    "brand": "brand",
    "model": "model",
    "trim / edition": "trim",
    "trim/edition": "trim",
    "edition": "trim",
    # ikman uses a different label per category: car listings say "Year of
    # Manufacture", van listings say "Model year". Missing the second cost
    # 763 rows (20% of the dataset) in the first export.
    "year of manufacture": "year",
    "model year": "year",
    "year": "year",
    "condition": "condition",
    "transmission": "transmission",
    "body type": "body_type",
    "fuel type": "fuel_type",
    "engine capacity": "engine_cc",
    "mileage": "mileage",
}


def extract_label_values(soup: BeautifulSoup) -> dict:
    """
    Label-driven extraction.

    ikman ships hashed CSS class names that change between deploys, so
    anchoring on classes would be brittle by construction. The visible labels
    ("Brand:", "Year of Manufacture:") are far more stable, so they drive the
    parse. Two structural strategies, then a regex fallback.
    """
    out: dict[str, str] = {}
    labels = set(DETAIL_LABELS)

    def consider(label_txt: str, value_txt: str) -> None:
        lab = label_txt.rstrip(":").strip().lower()
        val = normalise_ws(value_txt)
        if lab in DETAIL_LABELS and val and val.rstrip(":").strip().lower() not in labels:
            out.setdefault(DETAIL_LABELS[lab], val)

    # Strategy 1: label and value as adjacent siblings.
    for el in soup.find_all(["div", "span", "dt", "td", "li", "p"]):
        txt = normalise_ws(el.get_text(" ", strip=True))
        if txt.rstrip(":").strip().lower() not in labels:
            continue
        sib = el.find_next_sibling()
        if sib:
            consider(txt, sib.get_text(" ", strip=True))

    # Strategy 2: label and value inside a shared parent.
    if len(out) < 5:
        for el in soup.find_all(["div", "span", "dt", "td", "li", "p"]):
            txt = normalise_ws(el.get_text(" ", strip=True))
            if txt.rstrip(":").strip().lower() not in labels:
                continue
            parent = el.parent
            if not parent:
                continue
            whole = normalise_ws(parent.get_text(" ", strip=True))
            after = whole[len(txt):].strip() if whole.startswith(txt) else ""
            if after:
                consider(txt, after)

    # Strategy 3: regex over flattened page text.
    if len(out) < 5:
        text = normalise_ws(soup.get_text(" ", strip=True))
        for lab, key in DETAIL_LABELS.items():
            if key in out:
                continue
            m = re.search(
                re.escape(lab) + r"\s*:?\s*([A-Za-z0-9][\w\s/.,()-]{0,40}?)(?=\s+(?:"
                + "|".join(re.escape(x.title()) for x in DETAIL_LABELS)
                + r"|Description)\b)",
                text,
                re.IGNORECASE,
            )
            if m:
                out[key] = normalise_ws(m.group(1))
    return out


def meta_content(soup, prop) -> str:
    tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
    return normalise_ws(tag.get("content", "")) if tag else ""


def parse_detail(html: str, url: str, body_type_hint: str, current_year: int) -> Listing:
    soup = BeautifulSoup(html, "html.parser")
    fields = extract_label_values(soup)
    warnings: list[str] = []
    text = normalise_ws(soup.get_text(" ", strip=True))

    listing_id = urllib.parse.urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]

    # --- price ---------------------------------------------------------
    # IMPORTANT semantic difference from riyasewana. There, a listing shows
    # EITHER a figure OR the word "Negotiable", so is_negotiable implies a
    # missing target. Here, ikman shows a figure AND may additionally tag it
    # "Negotiable" — so a negotiable ikman listing still has a usable price.
    # Conflating the two would silently discard most of this site's data.
    price_raw = ""
    m = re.search(r"Rs\s*[\d,]{4,}", text)
    if m:
        price_raw = m.group(0)
    else:
        desc = meta_content(soup, "og:description")
        m = re.search(r"Rs\.?\s*[\d,]{4,}", desc)
        if m:
            price_raw = m.group(0)

    price_lkr, _ = parse_price(price_raw)
    is_negotiable = bool(re.search(r"\bnegotiable\b", text, re.IGNORECASE))
    if price_lkr is None:
        warnings.append("price_unparsed")

    year = parse_year(fields.get("year"))
    mileage_raw = fields.get("mileage", "")
    mileage_km = parse_mileage(mileage_raw)
    condition = normalise_condition(fields.get("condition", ""))

    flag = flag_suspicious_mileage(mileage_km, year, condition, current_year)
    if flag:
        warnings.append(flag)
    for req in ("brand", "model", "year"):
        if not fields.get(req):
            warnings.append(f"missing_{req}")

    # ikman records body type per listing; prefer it over the category hint.
    body_type = normalise_body_type(fields.get("body_type", "")) or body_type_hint

    seller_name = ""
    m = re.search(r"For sale by\s+([A-Za-z][^\n]{1,50}?)\s+(?:0\d|Click|Chat)", text)
    if m:
        seller_name = normalise_ws(m.group(1))

    views = None
    m = re.search(r"([\d,]+)\s*views", text, re.IGNORECASE)
    if m:
        views = parse_int(m.group(1))

    ad_date = ""
    m = re.search(r"Posted on\s+([\w\s:]+?(?:am|pm))", text, re.IGNORECASE)
    if m:
        ad_date = normalise_ws(m.group(1))

    # Location has no "Location:" label on ikman — it trails the posting date
    # as "Posted on <date>, <city>, <district>". Note the cross-site
    # asymmetry: ikman yields city AND district, riyasewana yields only a
    # city name. Both are stored verbatim; Phase 3 reconciles them (ikman's
    # final comma-component is the district; riyasewana needs a city lookup).
    location = ""
    m = re.search(
        r"Posted on\s+[\w\s:]+?(?:am|pm)\s*,\s*(.+?)\s+[\d,]+\s*views",
        text,
        re.IGNORECASE,
    )
    if m:
        # "Polgasowita , Colombo" -> "Polgasowita, Colombo"
        location = re.sub(r"\s*,\s*", ", ", normalise_ws(m.group(1))).strip(" ,")
    if not location:
        m = re.search(r"for Sale in ([A-Za-z\s]{2,40})\s*\|", meta_content(soup, "og:title"))
        if m:
            location = normalise_ws(m.group(1))
    if not location:
        warnings.append("missing_location")

    description = ""
    m = re.search(r"\bDescription\s+(.{20,4000}?)\s+For sale by\b", text, re.IGNORECASE)
    if m:
        description = normalise_ws(m.group(1))

    return Listing(
        listing_id=listing_id,
        source_site=SOURCE,
        url=url,
        scrape_timestamp=dt.datetime.now().isoformat(timespec="seconds"),
        ad_date=ad_date,
        title=meta_content(soup, "og:title") or (soup.h1.get_text(" ", strip=True) if soup.h1 else ""),
        brand=fields.get("brand", ""),
        model=fields.get("model", ""),
        trim=fields.get("trim", ""),
        year=year,
        price_lkr=price_lkr,
        price_raw=price_raw,
        is_negotiable=is_negotiable,
        mileage_km=mileage_km,
        mileage_raw=mileage_raw,
        fuel_type=fields.get("fuel_type", ""),
        transmission=fields.get("transmission", ""),
        engine_cc=parse_int(fields.get("engine_cc")),
        body_type=body_type,
        condition=condition,
        location=location,
        options="",
        description=description,
        seller_hash=hash_seller(seller_name),
        is_dealer_guess=(True if "MEMBER" in text else guess_dealer(seller_name)),
        is_promoted=bool(re.search(r"\b(FEATURED|Top ad)\b", text)),
        views=views,
        parse_warnings=warnings,
    )


# --------------------------------------------------------------------------
# Fetch / export / probe
# --------------------------------------------------------------------------

def fetch_details(limit: int, delay: float, max_cooldowns: int = 0) -> None:
    conn, session = db_connect(), make_session()
    rows = conn.execute(
        """
        SELECT lu.url, lu.body_type FROM listing_urls lu
        LEFT JOIN pages p ON p.url = lu.url
        WHERE p.url IS NULL OR p.status != 200
        ORDER BY RANDOM() LIMIT ?
        """,
        (limit,),
    ).fetchall()

    print(f"{len(rows)} listing pages to fetch (delay {delay}s)")
    ok = bad = throttled = consecutive = cooldowns = 0
    backoff = delay

    for i, (url, _bt) in enumerate(rows, 1):
        _html, status, retry_after = fetch(conn, url, session, delay)
        if status == 429:
            throttled += 1
            consecutive += 1
            if retry_after and retry_after > MAX_INLINE_SLEEP:
                resume_at = dt.datetime.now() + dt.timedelta(seconds=retry_after)
                print(f"\nSTOPPED: server sent Retry-After {retry_after}s "
                      f"(~{retry_after/3600:.1f} hours) — a hard rate limit.\n"
                      f"  {ok} fetched this run; all progress cached.\n"
                      f"  Resume after {resume_at:%Y-%m-%d %H:%M}.")
                break
            backoff = retry_after or min(backoff * 2, 300)
            print(f"  429 at {i}/{len(rows)} — backing off {backoff:.0f}s (consecutive {consecutive})")
            if consecutive >= MAX_CONSECUTIVE_429:
                if cooldowns < max_cooldowns:
                    cooldowns += 1
                    print(f"\n  standing down {COOLDOWN_SECONDS // 60} min "
                          f"(cooldown {cooldowns}/{max_cooldowns}), {ok} fetched")
                    time.sleep(COOLDOWN_SECONDS)
                    consecutive, backoff = 0, delay
                    continue
                print(f"\nSTOPPED after {consecutive} consecutive 429s. {ok} fetched, all cached.")
                break
            time.sleep(backoff)
            continue
        consecutive, backoff = 0, delay
        ok += 1 if status == 200 else 0
        bad += 0 if status == 200 else 1
        if i % 25 == 0:
            print(f"  {i}/{len(rows)}  ok={ok} failed={bad} throttled={throttled}")

    print(f"done: {ok} fetched, {bad} failed, {throttled} throttled")


def export(out_path: Path) -> None:
    conn = db_connect()
    current_year = dt.date.today().year
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = conn.execute(
        """
        SELECT p.url, p.html, COALESCE(lu.body_type,'')
        FROM pages p LEFT JOIN listing_urls lu ON lu.url = p.url
        WHERE p.status = 200 AND p.url LIKE '%/en/ad/%'
        """
    ).fetchall()

    written = failed = 0
    warn: dict[str, int] = {}
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CANONICAL_FIELDS)
        w.writeheader()
        for url, html, bt in rows:
            try:
                L = parse_detail(html, url, bt, current_year)
            except Exception as exc:  # noqa: BLE001
                print(f"  ! parse failed {url}: {exc}", file=sys.stderr)
                failed += 1
                continue
            for x in L.parse_warnings:
                warn[x] = warn.get(x, 0) + 1
            w.writerow(L.to_row())
            written += 1

    print(f"\nwrote {written} rows to {out_path} ({failed} parse failures)")
    if warn:
        print("\nparse warnings:")
        for k, v in sorted(warn.items(), key=lambda x: -x[1]):
            print(f"  {v:6d}  {k}")


def compact() -> None:
    """
    Shrink an existing cache in place by stripping already-stored pages.

    Safe to run repeatedly. VACUUM is required afterwards or SQLite keeps the
    freed pages allocated and the file never actually shrinks on disk.
    """
    conn = db_connect()
    before = DB_PATH.stat().st_size
    rows = conn.execute(
        "SELECT url, LENGTH(html) FROM pages WHERE html IS NOT NULL"
    ).fetchall()
    print(f"compacting {len(rows)} cached pages ({before / 1e9:.2f} GB)")

    changed = 0
    for i, (url, _n) in enumerate(rows, 1):
        html = conn.execute("SELECT html FROM pages WHERE url=?", (url,)).fetchone()[0]
        stripped = strip_boilerplate(html)
        if len(stripped) < len(html):
            conn.execute("UPDATE pages SET html=? WHERE url=?", (stripped, url))
            changed += 1
        if i % 500 == 0:
            conn.commit()
            print(f"  {i}/{len(rows)}")
    conn.commit()

    print("  running VACUUM (this takes a minute)...")
    conn.execute("VACUUM")
    conn.close()

    after = DB_PATH.stat().st_size
    print(f"\ndone: {changed} pages stripped")
    print(f"  {before / 1e9:.2f} GB -> {after / 1e9:.2f} GB "
          f"({100 * (1 - after / before):.0f}% smaller)")


def probe(url: str, delay: float) -> None:
    conn, session = db_connect(), make_session()
    html, status, _ = fetch(conn, url, session, delay, force=True)
    if not html or status != 200:
        print(f"fetch failed with status {status}")
        return
    L = parse_detail(html, url, "", dt.date.today().year)
    print(f"\n--- parsed from {url} ---")
    optional = {"options", "description", "trim", "parse_warnings"}
    for k, v in L.to_row().items():
        missing = v in ("", None) and k not in optional
        print(f"{'??' if missing else '  '} {k:18s} {v}")
    print("\n'??' marks fields the parser could not find. Check these before a full run.")


def main() -> None:
    ap = argparse.ArgumentParser(description="ikman.lk scraper")
    sub = ap.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("harvest")
    h.add_argument("--categories", nargs="+", default=["cars", "vans"])
    h.add_argument("--max-pages", type=int, default=120)
    h.add_argument("--source", choices=["pagination", "sitemap"], default="pagination")
    h.add_argument("--delay", type=float, default=DEFAULT_DELAY)

    f = sub.add_parser("fetch")
    f.add_argument("--limit", type=int, default=1000)
    f.add_argument("--max-cooldowns", type=int, default=0)
    f.add_argument("--delay", type=float, default=DEFAULT_DELAY)

    e = sub.add_parser("export")
    e.add_argument("--out", type=Path, default=Path("data/raw/ikman.csv"))

    sub.add_parser("compact")

    p = sub.add_parser("probe")
    p.add_argument("--url", required=True)
    p.add_argument("--delay", type=float, default=0.0)

    a = ap.parse_args()
    if a.cmd == "harvest":
        (harvest_sitemap(a.delay) if a.source == "sitemap"
         else harvest_pagination(a.categories, a.max_pages, a.delay))
    elif a.cmd == "fetch":
        fetch_details(a.limit, a.delay, a.max_cooldowns)
    elif a.cmd == "export":
        export(a.out)
    elif a.cmd == "compact":
        compact()
    elif a.cmd == "probe":
        probe(a.url, a.delay)


if __name__ == "__main__":
    main()
