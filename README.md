# S25021960 - COM763 Advanced Machine Learning

## Project Structure

```
├── data/           # Dataset files
├── src/            # Source code
    |── scrapers/
├── notebooks/      # Jupyter notebooks
└── README.md       # Project documentation
```


## Scrapers

### ikman.lk (3,724 listings)

```bash
# Probe a single ad to verify parsing
python src/scrapers/ikman.py probe --url https://ikman.lk/en/ad/<some-ad>

# Harvest listing URLs from pagination (default)
python src/scrapers/ikman.py harvest --categories cars vans --max-pages 120

# Alternative: harvest from sitemaps (deeper but ~9 days stale)
python src/scrapers/ikman.py harvest --source sitemap

# Fetch and parse all harvested listings
python src/scrapers/ikman.py fetch --limit 4000 --max-cooldowns 6

# Compact the database
python src/scrapers/ikman.py compact

# Export to CSV
python src/scrapers/ikman.py export --out data/raw/ikman.csv
```

**Compliance**: Respects robots.txt — blocks search/filter/sort/query URLs and double-hyphen URLs. Pagination (`?page=N`) is allowed.

### riyasewana.com (488 listings)

```bash
# Probe a single ad to verify parsing
python src/scrapers/riyasewana.py probe --url https://riyasewana.com/buy/<some-ad>

# Harvest listing URLs by category
python src/scrapers/riyasewana.py harvest --categories cars suvs vans --max-pages 60

# Fetch and parse all harvested listings (rate-limited with jitter)
python src/scrapers/riyasewana.py fetch --limit 700 --delay 8 --max-cooldowns 6

# Export to CSV
python src/scrapers/riyasewana.py export --out data/raw/riyasewana.csv
```

**Compliance**: Respects robots.txt — six endpoints hard-blocked. All requests are rate-limited with jitter, and responses are cached so re-parsing never re-fetches.

### Shared Schema (`src/scrapers/schema.py`)

Both scrapers output a canonical schema with fields: `listing_id`, `source_site`, `url`, `scrape_timestamp`, `ad_date`, `title`, `brand`, `model`, `year`, `mileage`, `price`, `fuel_type`, `transmission`, `body_type`, `engine_capacity`, `location`, and more. The schema module parses but does not clean — implausible values are flagged, not silently corrected.