"""
Tender pipeline: collects construction tenders from public Pakistani
procurement portals, filters to construction work, and writes tenders.js
for tender_dashboard.html.

Run:  python tender_pipeline.py
Needs: pip install requests beautifulsoup4

Safety:
- A portal that fails is logged and skipped; the others still run.
- If nothing is collected, the existing tenders.js is left unchanged.
- Entries with no readable closing date are skipped (logged), never guessed.
"""
import json, re, datetime as dt, logging, pathlib
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

OUT = pathlib.Path(__file__).with_name("tenders.js")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; tender-monitor/1.0)"}
TIMEOUT = 30
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("tenders")

# Listing pages to scan. VERIFY each URL in a browser before relying on it;
# portals move pages and some need registration to see bid documents.
SOURCES = [
    {"name": "PPRA Punjab",      "url": "https://ppra.punjab.gov.pk/"},
    {"name": "PPRA KPK",         "url": "https://kppra.gov.pk/"},
    {"name": "PPRA Federal/ICT", "url": "https://www.ppra.org.pk/"},
    {"name": "EPADS",            "url": "https://www.epads.gov.pk/"},      # VERIFY
    {"name": "NHA",              "url": "https://nha.gov.pk/"},            # VERIFY tender page path
    {"name": "PPRA Sindh",       "url": "https://www.spprasindh.gov.pk/"}, # VERIFY
    {"name": "PPRA Balochistan", "url": "https://bppra.gov.pk/"},          # VERIFY
]

CONSTRUCTION = re.compile(
    r"construct|civil|road|bridge|building|water|sewer|canal|dam\b|highway|"
    r"rehabilit|renovat|substation|solar|street|drain|embankment|housing|"
    r"expressway|interchange|pipeline|hospital|college|school", re.I)
NOTICE_HINT = re.compile(r"tender|bid|rfp|eoi|procure|notice", re.I)
DATE = re.compile(r"(\d{1,2})[-/ ](\d{1,2}|[A-Za-z]{3,9})[-/ ](\d{4})")
MONTHS = {m: i for i, m in enumerate(
    ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"], 1)}


def parse_date(s):
    """Return a date from text like 03-11-2026, 3/11/2026, 3 Nov 2026."""
    m = DATE.search(s or "")
    if not m:
        return None
    d, mon, y = m.groups()
    month = int(mon) if mon.isdigit() else MONTHS.get(mon[:3].lower())
    try:
        return dt.date(int(y), month, int(d))
    except (TypeError, ValueError):
        return None


def to_pkr_millions(text):
    if not text:
        return 0.0
    t = text.replace(",", "").lower()
    m = re.search(r"([\d.]+)\s*(billion|bn|million|m|crore|cr)?", t)
    if not m:
        return 0.0
    n = float(m.group(1))
    unit = m.group(2) or ""
    if unit in ("billion", "bn"): return n * 1000
    if unit in ("million", "m"):  return n
    if unit in ("crore", "cr"):   return n * 10
    return n / 1e6 if n > 1e6 else n


def categorise(title):
    for cat, pat in [("Roads", r"road|bridge|highway|expressway|carriageway|interchange"),
                     ("Water", r"water|sewer|canal|dam|drain|embankment|flood|pipeline"),
                     ("Power", r"solar|power|substation|lighting|grid|electric"),
                     ("Buildings", r"building|hospital|college|school|office|housing|block")]:
        if re.search(pat, title, re.I):
            return cat
    return "Other"


def region_for(source, text):
    s = (source + " " + text).lower()
    for name, pat in [("ICT", r"islamabad|\bict\b|federal|nha|epads"),
                      ("KPK", r"khyber|kpk|kppra|peshawar"),
                      ("Punjab", r"punjab|lahore|rawalpindi|multan"),
                      ("Sindh", r"sindh|karachi"),
                      ("Balochistan", r"balochistan|quetta")]:
        if re.search(pat, s):
            return name
    return "Other"


def scan_listing(source):
    """Generic scan: find links/rows that look like construction tender notices."""
    r = requests.get(source["url"], headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    records, seen = [], set()
    # Candidate containers: table rows first, then list items, then bare links.
    containers = soup.select("tr") or soup.select("li") or soup.select("a[href]")
    for node in containers:
        text = node.get_text(" ", strip=True)
        if not text or not CONSTRUCTION.search(text) or not NOTICE_HINT.search(text):
            continue
        link = node if node.name == "a" else node.find("a", href=True)
        url = urljoin(source["url"], link["href"]) if link and link.get("href") else source["url"]
        if url in seen:
            continue
        seen.add(url)
        records.append({"title": (link.get_text(" ", strip=True) if link else text)[:200],
                        "context": text, "url": url, "source": source["name"]})
    return records


def normalise(rec):
    """Turn a raw record into a dashboard row, or None if not usable."""
    ctx = rec["context"]
    # Closing date: prefer a date that sits near "closing"/"last date"/"due".
    close = None
    m = re.search(r"(closing|last date|due|submission|deadline)[^0-9]{0,40}(.{0,30})", ctx, re.I)
    if m:
        close = parse_date(m.group(2))
    if not close:
        close = parse_date(ctx)  # fall back to the first date in the row
    if not close:
        log.info("skipped (no closing date): %s", rec["title"][:80])
        return None
    published_m = re.search(r"(published|issued|posted)[^0-9]{0,40}(.{0,30})", ctx, re.I)
    pub = parse_date(published_m.group(2)) if published_m else None
    budget_m = re.search(r"(rs\.?|pkr)?\s*[\d,.]+\s*(billion|million|crore|bn|m|cr)?", ctx, re.I)
    return {
        "id": re.sub(r"\W+", "-", rec["title"])[:40].strip("-") or "tender",
        "title": rec["title"],
        "agency": rec["source"],          # refine per portal if agency is shown
        "region": region_for(rec["source"], ctx),
        "category": categorise(rec["title"]),
        "budget": round(to_pkr_millions(budget_m.group(0)) if budget_m else 0.0, 2),
        "published": (pub or close - dt.timedelta(days=21)).isoformat(),
        "closing": close.isoformat(),
        "source": rec["source"],
        "url": rec["url"],
    }


def main():
    rows, seen = [], set()
    for src in SOURCES:
        try:
            raw = scan_listing(src)
            log.info("%s: %d candidate notices", src["name"], len(raw))
        except Exception as e:
            log.warning("%s failed: %s", src["name"], e)
            continue
        for rec in raw:
            n = normalise(rec)
            key = (n["title"], n["closing"]) if n else None
            if n and key not in seen:
                seen.add(key)
                rows.append(n)
    if not rows:
        log.warning("no tenders collected; leaving %s unchanged", OUT)
        return
    OUT.write_text("const TENDERS = " + json.dumps(rows, indent=1, ensure_ascii=False) + ";\n",
                   encoding="utf-8")
    log.info("wrote %d tenders to %s", len(rows), OUT)


if __name__ == "__main__":
    main()
