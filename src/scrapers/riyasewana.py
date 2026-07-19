"""
riyasewana.com scraper — Phase 2.

Two stages, run separately so a failure in one does not cost you the other:

    python scrapers/riyasewana.py harvest --categories cars suvs vans --max-pages 60
    python scrapers/riyasewana.py fetch --limit 2000
    python scrapers/riyasewana.py export --out data/raw/riyasewana.csv

Verify the parser before committing to a long run:

    python scrapers/riyasewana.py probe --url https://riyasewana.com/buy/<some-ad>

Compliance (robots.txt retrieved 2026-07-18):
  riyasewana.com allows general crawling but disallows six endpoints, which
  are hard-blocked in BLOCKED_PATHS below. Requests are rate-limited with
  jitter and every response is cached, so re-parsing never re-fetches.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import random
import re
import sqlite3
import sys
import time
import urllib.parse
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
)

BASE = "https://riyasewana.com"
SOURCE = "riyasewana"

# Explicitly disallowed by robots.txt — never request these.
BLOCKED_PATHS = (
    "/vehicle_more.php",
    "/login.php",
    "/add-favorite.php",
    "/get-phone.php",
    "/get-price-range.php",
    "/sug.php",
)

# Set a real contact address before running. An honest, identifiable
# User-Agent is part of scraping responsibly, and it is what you will be
# describing in the report's ethics subsection.
USER_AGENT = (
    "MScResearchBot/1.0 (postgraduate coursework; vehicle price research; "
    "contact: t.perera@hayleysadvantis.com)"
)

CATEGORY_BODY_TYPE = {"cars": "car", "suvs": "suv", "vans": "van"}

DB_PATH = Path("data/raw/riyasewana_cache.sqlite")

# Observed 2026-07-18: riyasewana began returning 429 after ~700 requests at
# a 1.5s delay. 4s is the new default; raise it further if throttling recurs.
DEFAULT_DELAY = 4.0
MAX_CONSECUTIVE_429 = 5
# Never sleep longer than this inside a run. A Retry-After above it means the
# site has hard-limited us; report and exit rather than hang for hours.
# riyasewana returned Retry-After: 85687 (~24h) on 2026-07-18.
MAX_INLINE_SLEEP = 900
# How long to stand down when the server has clearly had enough. Used only
# in cooldown mode (--max-cooldowns), for long unattended runs.
COOLDOWN_SECONDS = 1800

DETAIL_LABELS = {
    "location": "location",
    "year": "year",
    "mileage": "mileage",
    "make": "make",
    "model": "model",
    "gear": "gear",
    "fuel type": "fuel_type",
    "engine (cc)": "engine_cc",
    "engine": "engine_cc",
    "condition": "condition",
    "ad date": "ad_date",
    "options": "options",
    "details": "description",
    "more details": "description",
    "price": "price",
}


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

def db_connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS pages (
            url           TEXT PRIMARY KEY,
            html          TEXT,
            status        INTEGER,
            fetched_at    TEXT
        );
        CREATE TABLE IF NOT EXISTS listing_urls (
            url           TEXT PRIMARY KEY,
            body_type     TEXT,
            discovered_at TEXT
        );
        """
    )
    conn.commit()
    return conn


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def is_blocked(url: str) -> bool:
    path = urllib.parse.urlparse(url).path
    return any(path.startswith(b) for b in BLOCKED_PATHS)


def fetch(
    conn: sqlite3.Connection,
    url: str,
    session: requests.Session,
    delay: float = DEFAULT_DELAY,
    force: bool = False,
) -> tuple[str | None, int, int | None]:
    """
    Fetch with caching. Returns (html, status, retry_after).

    Cached 200s cost nothing. `retry_after` is populated only when the server
    sends a Retry-After header alongside a 429, so the caller can honour the
    server's own stated backoff rather than guessing.
    """
    if is_blocked(url):
        raise ValueError(f"Refusing to fetch robots.txt-disallowed URL: {url}")

    if not force:
        row = conn.execute(
            "SELECT html, status FROM pages WHERE url = ?", (url,)
        ).fetchone()
        if row and row[1] == 200:
            return row[0], row[1], None

    # Jitter avoids a perfectly regular request pattern.
    time.sleep(delay + random.uniform(0, delay * 0.5))

    retry_after = None
    try:
        resp = session.get(url, timeout=30)
        html, status = resp.text, resp.status_code
        if status == 429:
            # Server is explicitly asking us to slow down. Never store the
            # error body as if it were listing content.
            html = None
            ra = resp.headers.get("Retry-After")
            if ra and ra.isdigit():
                retry_after = int(ra)
    except requests.RequestException as exc:
        print(f"  ! request failed {url}: {exc}", file=sys.stderr)
        html, status = None, 0

    conn.execute(
        "INSERT OR REPLACE INTO pages (url, html, status, fetched_at) VALUES (?,?,?,?)",
        (url, html, status, dt.datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    return html, status, retry_after


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en"})
    return s


# --------------------------------------------------------------------------
# Stage 1 — harvest listing URLs from index pages
# --------------------------------------------------------------------------

AD_URL_RE = re.compile(r"^https?://riyasewana\.com/buy/[\w\-]+-(\d+)$")


def harvest(categories: list[str], max_pages: int, delay: float) -> None:
    conn = db_connect()
    session = make_session()
    total_new = 0

    for cat in categories:
        body_type = CATEGORY_BODY_TYPE.get(cat, cat)
        print(f"\n== harvesting {cat} ({body_type}) ==")

        for page in range(1, max_pages + 1):
            url = f"{BASE}/search/{cat}" + (f"?page={page}" if page > 1 else "")
            html, status, _ = fetch(conn, url, session, delay)
            if not html or status != 200:
                print(f"  page {page}: status {status}, stopping category")
                break

            soup = BeautifulSoup(html, "html.parser")
            found = set()
            for a in soup.find_all("a", href=True):
                href = urllib.parse.urljoin(BASE, a["href"]).split("?")[0]
                if AD_URL_RE.match(href):
                    found.add(href)

            if not found:
                print(f"  page {page}: no ad links found — stopping category")
                break

            new = 0
            for ad_url in found:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO listing_urls (url, body_type, discovered_at)"
                    " VALUES (?,?,?)",
                    (ad_url, body_type, dt.datetime.now().isoformat(timespec="seconds")),
                )
                new += cur.rowcount
            conn.commit()
            total_new += new
            print(f"  page {page}: {len(found)} links, {new} new")

    n = conn.execute("SELECT COUNT(*) FROM listing_urls").fetchone()[0]
    print(f"\nharvest complete: {total_new} new, {n} total listing URLs known")


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def extract_label_values(soup: BeautifulSoup) -> dict:
    """
    Pull the detail block into a dict.

    Strategy 0 targets riyasewana's actual markup, confirmed against a live
    page on 2026-07-18:

        <div class="detail-row">
          <span class="detail-label">Make</span>
          <span class="detail-value">Toyota</span>
        </div>

    Strategies 1-3 are generic fallbacks that survive a markup change, since
    the human-visible labels are more stable than the class names.

    Note the `not in DETAIL_LABELS` guard on the value in every positional
    strategy. Without it, two labels sitting next to each other get paired as
    label/value — which is exactly how an early version read the price card
    ("Price", "Contact") and returned "Contact" as the price.
    """
    out: dict[str, str] = {}

    # Strategy 0: the real markup.
    for row in soup.select("div.detail-row"):
        label_el = row.select_one(".detail-label")
        value_el = row.select_one(".detail-value")
        if label_el and value_el:
            label = normalise_ws(label_el.get_text(" ", strip=True)).rstrip(":").lower()
            value = normalise_ws(value_el.get_text(" ", strip=True))
            if label in DETAIL_LABELS and value:
                out.setdefault(DETAIL_LABELS[label], value)

    # Strategy 1: table rows of alternating label/value cells.
    if len(out) < 4:
        for tr in soup.find_all("tr"):
            cells = [normalise_ws(c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"])]
            for i in range(len(cells) - 1):
                label = cells[i].rstrip(":").strip().lower()
                value = cells[i + 1]
                if label in DETAIL_LABELS and value and value.strip().lower() not in DETAIL_LABELS:
                    out.setdefault(DETAIL_LABELS[label], value)

    # Strategy 2: definition lists / adjacent inline label-value pairs.
    if len(out) < 4:
        for container in soup.find_all(["dl", "ul", "div"]):
            items = [
                normalise_ws(x.get_text(" ", strip=True))
                for x in container.find_all(["dt", "dd", "li", "span", "p", "div"], recursive=True)
            ]
            for i in range(len(items) - 1):
                label = items[i].rstrip(":").strip().lower()
                value = items[i + 1]
                if label in DETAIL_LABELS and value and value.strip().lower() not in DETAIL_LABELS:
                    out.setdefault(DETAIL_LABELS[label], value)

    # Strategy 3: regex over page text, for labels glued to their values.
    if len(out) < 4:
        text = normalise_ws(soup.get_text(" ", strip=True))
        patterns = {
            "year": r"Year\s*(\d{4})",
            "mileage": r"Mileage\s*([\d,]+\s*km)",
            "make": r"Make\s*([A-Za-z\-]+)",
            "model": r"Model\s*([A-Za-z0-9\- ]{1,40}?)\s*(?:Gear|Fuel|Engine|Condition)",
            "gear": r"Gear\s*(Automatic|Manual|Tiptronic|Other)",
            "fuel_type": r"Fuel Type\s*(Petrol|Diesel|Hybrid|Electric|Gas)",
            "engine_cc": r"Engine \(cc\)\s*([\d,]+)",
            "condition": r"Condition\s*([A-Za-z()\s]{3,30}?)\s*(?:Ad Date|Options|More)",
        }
        for key, pat in patterns.items():
            if key not in out:
                m = re.search(pat, text, re.IGNORECASE)
                if m:
                    out[key] = normalise_ws(m.group(1))

    return out


def meta_content(soup: BeautifulSoup, prop: str) -> str:
    tag = soup.find("meta", attrs={"property": prop}) or soup.find(
        "meta", attrs={"name": prop}
    )
    return normalise_ws(tag.get("content", "")) if tag else ""


def extract_price_raw(soup: BeautifulSoup) -> str:
    """
    Price lives in its own card, not in the detail rows:

        <div class="price-section">
          <span class="price-label">Price</span>
          <div class="price-amount">Rs. 14,450,000</div>
        </div>

    Tried in order: the real element, the meta description, then page text.
    """
    el = soup.select_one("div.price-amount, .price-amount")
    if el:
        value = normalise_ws(el.get_text(" ", strip=True))
        if value:
            return value

    desc = meta_content(soup, "og:description") or meta_content(soup, "description")
    mm = re.search(r"Rs\.?\s*[\d,]{4,}", desc)
    if mm:
        return mm.group(0)
    if re.search(r"negotiab", desc, re.IGNORECASE):
        return "Negotiable"

    text = soup.get_text(" ", strip=True)
    mm = re.search(r"Price\s*(Rs\.?\s*[\d,]{4,}|Negotiable)", text, re.IGNORECASE)
    return mm.group(1) if mm else ""


def extract_more_card(soup: BeautifulSoup, title: str) -> str:
    """Read a titled 'more-card' block (Options / More Details) by its heading."""
    for card in soup.select("div.more-card"):
        heading = card.select_one(".more-card-title")
        if not heading:
            continue
        if normalise_ws(heading.get_text(" ", strip=True)).lower() != title.lower():
            continue
        chips = card.select(".option-chip")
        if chips:
            return "|".join(normalise_ws(c.get_text(" ", strip=True)) for c in chips)
        body = card.select_one(".more-card-body")
        if body:
            return normalise_ws(body.get_text(" ", strip=True))
    return ""


def parse_detail(html: str, url: str, body_type: str, current_year: int) -> Listing:
    soup = BeautifulSoup(html, "html.parser")
    fields = extract_label_values(soup)
    warnings: list[str] = []

    m = AD_URL_RE.match(url)
    listing_id = m.group(1) if m else url.rsplit("-", 1)[-1]

    price_raw = extract_price_raw(soup) or fields.get("price", "")
    price_lkr, is_negotiable = parse_price(price_raw)
    if price_lkr is None and not is_negotiable:
        warnings.append("price_unparsed")

    # --- seller: hashed, never stored raw (see schema.hash_seller) ---
    seller_name = ""
    seller_el = soup.select_one(".seller-info")
    seller_text = (
        normalise_ws(seller_el.get_text(" ", strip=True))
        if seller_el
        else soup.get_text("\n", strip=True)
    )
    mm = re.search(r"Posted by\s+([^\n·,]{2,60})", seller_text)
    if mm:
        seller_name = normalise_ws(mm.group(1))

    is_promoted = soup.select_one(".premium-badge") is not None
    views_el = soup.select_one(".views-count")
    views = parse_int(views_el.get_text(" ", strip=True)) if views_el else None

    year = parse_year(fields.get("year"))
    mileage_raw = fields.get("mileage", "")
    mileage_km = parse_mileage(mileage_raw)
    # Normalised to the canonical vocabulary so riyasewana's "Registered
    # (Used)" and ikman's "Used" become the same value. Without this the
    # two sources cannot be pooled or compared.
    condition_raw = fields.get("condition", "")
    condition = normalise_condition(condition_raw)

    flag = flag_suspicious_mileage(mileage_km, year, condition, current_year)
    if flag:
        warnings.append(flag)

    for required in ("make", "model", "year"):
        if not fields.get(required):
            warnings.append(f"missing_{required}")

    title = meta_content(soup, "og:title")
    if not title:
        h1 = soup.find("h1")
        title = normalise_ws(h1.get_text(" ", strip=True)) if h1 else ""

    return Listing(
        listing_id=listing_id,
        source_site=SOURCE,
        url=url,
        scrape_timestamp=dt.datetime.now().isoformat(timespec="seconds"),
        ad_date=fields.get("ad_date", ""),
        title=title,
        brand=fields.get("make", ""),
        model=fields.get("model", ""),
        year=year,
        price_lkr=price_lkr,
        price_raw=price_raw,
        is_negotiable=is_negotiable,
        mileage_km=mileage_km,
        mileage_raw=mileage_raw,
        fuel_type=fields.get("fuel_type", ""),
        transmission=fields.get("gear", ""),
        engine_cc=parse_int(fields.get("engine_cc")),
        body_type=normalise_body_type(body_type) or body_type,
        condition=condition,
        location=fields.get("location", ""),
        options=extract_more_card(soup, "Options") or fields.get("options", ""),
        description=extract_more_card(soup, "More Details") or fields.get("description", ""),
        seller_hash=hash_seller(seller_name),
        is_dealer_guess=guess_dealer(seller_name),
        is_promoted=is_promoted,
        views=views,
        parse_warnings=warnings,
    )


# --------------------------------------------------------------------------
# Stage 2 — fetch detail pages
# --------------------------------------------------------------------------

def fetch_details(limit: int, delay: float, max_cooldowns: int = 0) -> None:
    conn = db_connect()
    session = make_session()
    current_year = dt.date.today().year

    # ORDER BY RANDOM() matters. Harvest inserts cars, then SUVs, then vans,
    # so taking URLs in insertion order means a partial fetch returns cars
    # only — the first 500-row sample was 100% cars despite the harvest being
    # near-evenly split three ways. Randomising keeps every partial run
    # representative, so early quality checks generalise to the full dataset.
    rows = conn.execute(
        """
        SELECT lu.url, lu.body_type
        FROM listing_urls lu
        LEFT JOIN pages p ON p.url = lu.url
        WHERE p.url IS NULL OR p.status != 200
        ORDER BY RANDOM()
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    print(f"{len(rows)} listing pages to fetch (delay {delay}s)")
    ok = bad = throttled = 0
    consecutive_429 = 0
    cooldowns_used = 0
    backoff = delay

    for i, (url, body_type) in enumerate(rows, 1):
        html, status, retry_after = fetch(conn, url, session, delay)

        if status == 429:
            # Back off exponentially, honouring Retry-After when the server
            # sends one. Continuing to hammer a throttled server is both
            # futile and the fastest way to turn a soft limit into a ban.
            throttled += 1
            consecutive_429 += 1

            # A long Retry-After is not something to sleep through. riyasewana
            # returned 85,687s (~24h) on 2026-07-18, and sleeping on that
            # blocks the process for a day while looking like a hang. Report
            # it and exit so the run can be rescheduled deliberately.
            if retry_after and retry_after > MAX_INLINE_SLEEP:
                resume_at = dt.datetime.now() + dt.timedelta(seconds=retry_after)
                print(
                    f"\nSTOPPED: server sent Retry-After {retry_after}s "
                    f"(~{retry_after / 3600:.1f} hours).\n"
                    f"  This is a hard rate limit, not a transient throttle.\n"
                    f"  {ok} fetched this run; all progress is cached.\n"
                    f"  Resume after {resume_at:%Y-%m-%d %H:%M}."
                )
                break

            backoff = retry_after or min(backoff * 2, 300)
            print(
                f"  429 rate-limited at {i}/{len(rows)} "
                f"— backing off {backoff:.0f}s (consecutive: {consecutive_429})"
            )
            if consecutive_429 >= MAX_CONSECUTIVE_429:
                if cooldowns_used < max_cooldowns:
                    cooldowns_used += 1
                    print(
                        f"\n  standing down for {COOLDOWN_SECONDS // 60} min "
                        f"(cooldown {cooldowns_used}/{max_cooldowns}), "
                        f"{ok} fetched so far — will resume automatically"
                    )
                    time.sleep(COOLDOWN_SECONDS)
                    consecutive_429 = 0
                    backoff = delay
                    continue
                print(
                    f"\nSTOPPED: {consecutive_429} consecutive 429s. The server is "
                    f"refusing us.\n  {ok} fetched this run. Progress is cached — "
                    f"rerun later with a larger --delay to resume.\n"
                    f"  Suggested: wait a few hours, then --delay {max(delay * 2, 8):.0f}"
                )
                break
            time.sleep(backoff)
            continue

        consecutive_429 = 0
        backoff = delay
        if html and status == 200:
            ok += 1
        else:
            bad += 1
        if i % 25 == 0:
            print(f"  {i}/{len(rows)}  ok={ok} failed={bad} throttled={throttled}")

    print(f"done: {ok} fetched, {bad} failed, {throttled} throttled")
    remaining = conn.execute(
        """
        SELECT COUNT(*) FROM listing_urls lu
        LEFT JOIN pages p ON p.url = lu.url
        WHERE p.url IS NULL OR p.status != 200
        """
    ).fetchone()[0]
    print(f"{remaining} listings still outstanding")


# --------------------------------------------------------------------------
# Stage 3 — export
# --------------------------------------------------------------------------

def export(out_path: Path) -> None:
    conn = db_connect()
    current_year = dt.date.today().year
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # LEFT JOIN so that pages fetched outside a harvest (e.g. by `probe`) are
    # still exported rather than silently dropped; body_type is simply unknown.
    rows = conn.execute(
        """
        SELECT p.url, p.html, COALESCE(lu.body_type, '')
        FROM pages p LEFT JOIN listing_urls lu ON lu.url = p.url
        WHERE p.status = 200 AND p.url LIKE '%/buy/%'
        """
    ).fetchall()

    written = failed = 0
    warn_counts: dict[str, int] = {}

    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CANONICAL_FIELDS)
        writer.writeheader()
        for url, html, body_type in rows:
            try:
                listing = parse_detail(html, url, body_type, current_year)
            except Exception as exc:  # noqa: BLE001 - report and continue
                print(f"  ! parse failed {url}: {exc}", file=sys.stderr)
                failed += 1
                continue
            for w in listing.parse_warnings:
                warn_counts[w] = warn_counts.get(w, 0) + 1
            writer.writerow(listing.to_row())
            written += 1

    print(f"\nwrote {written} rows to {out_path} ({failed} parse failures)")
    if warn_counts:
        print("\nparse warnings — carry these into Phase 3 cleaning:")
        for w, c in sorted(warn_counts.items(), key=lambda x: -x[1]):
            print(f"  {c:6d}  {w}")


# --------------------------------------------------------------------------
# Probe — verify the parser on a single page before a long run
# --------------------------------------------------------------------------

def probe(url: str, delay: float) -> None:
    conn = db_connect()
    session = make_session()
    html, status, _ = fetch(conn, url, session, delay, force=True)
    if not html or status != 200:
        print(f"fetch failed with status {status}")
        return

    listing = parse_detail(html, url, "car", dt.date.today().year)
    print(f"\n--- parsed from {url} ---")
    for k, v in listing.to_row().items():
        marker = "  " if v not in ("", None) else "??"
        print(f"{marker} {k:20s} {v}")
    print(
        "\nAny '??' above is a field the parser could not find. "
        "If core fields are missing, inspect the cached HTML in "
        f"{DB_PATH} before running a full fetch."
    )


# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="riyasewana.com scraper")
    sub = ap.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("harvest", help="collect listing URLs from index pages")
    h.add_argument("--categories", nargs="+", default=["cars", "suvs", "vans"])
    h.add_argument("--max-pages", type=int, default=60)
    h.add_argument("--delay", type=float, default=DEFAULT_DELAY)

    f = sub.add_parser("fetch", help="fetch detail pages for harvested URLs")
    f.add_argument("--limit", type=int, default=1000)
    f.add_argument("--max-cooldowns", type=int, default=0,
                   help="on repeated 429s, stand down 30 min and resume, up to N times")
    f.add_argument("--delay", type=float, default=DEFAULT_DELAY)

    e = sub.add_parser("export", help="parse cached pages to CSV")
    e.add_argument("--out", type=Path, default=Path("data/raw/riyasewana.csv"))

    p = sub.add_parser("probe", help="test the parser against one page")
    p.add_argument("--url", required=True)
    p.add_argument("--delay", type=float, default=0.0)

    args = ap.parse_args()
    if args.cmd == "harvest":
        harvest(args.categories, args.max_pages, args.delay)
    elif args.cmd == "fetch":
        fetch_details(args.limit, args.delay, args.max_cooldowns)
    elif args.cmd == "export":
        export(args.out)
    elif args.cmd == "probe":
        probe(args.url, args.delay)


if __name__ == "__main__":
    main()
