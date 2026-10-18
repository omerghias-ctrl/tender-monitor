"""
Tender pipeline: collects construction tenders from public portals,
normalises them, and writes tenders.js for tender_dashboard.html.

Usage:
    pip install requests beautifulsoup4
    python tender_pipeline.py          # writes tenders.js next to this file

Each source is an adapter. Portal layouts change, so each adapter must be
checked against the live site before you rely on it. Adapters that fail are
skipped and logged, never silently dropped.
"""
import json, re, datetime as dt, logging, pathlib, requests
from bs4 import BeautifulSoup

OUT = pathlib.Path(__file__).with_name("tenders.js")
HEADERS = {"User-Agent": "Mozilla/5.0 (tender-monitor; contact: you@example.com)"}
KEYWORDS = re.compile(r"construct|civil|road|bridge|building|water|sewer|canal|dam|"
                      r"highway|rehabilit|renovat|substation|solar|street", re.I)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("tenders")


def to_pkr_millions(text: str):
    """'Rs. 1,250,000,000' or 'PKR 12.5 Million' -> float millions."""
    if not text:
        return None
    t = text.replace(",", "").lower()
    m = re.search(r"([\d.]+)\s*(billion|bn|million|m|crore|cr)?", t)
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2) or ""
    if unit in ("billion", "bn"):
        return n * 1000
    if unit in ("million", "m"):
        return n
    if unit in ("crore", "cr"):
        return n * 10
    return n / 1e6 if n > 1e6 else n  # bare rupee amount


def categorise(title: str) -> str:
    t = title.lower()
    for cat, pat in [("Roads", r"road|bridge|highway|expressway|carriageway|interchange"),
                     ("Water", r"water|sewer|canal|dam|drain|embankment|flood"),
                     ("Power", r"solar|power|substation|lighting|grid|electric"),
                     ("Buildings", r"building|hospital|college|school|office|housing|block")]:
        if re.search(pat, t):
            return cat
    return "Other"


def region_for(source: str, text: str) -> str:
    s = (source + " " + text).lower()
    for name, pat in [("ICT", r"islamabad|\bict\b"), ("KPK", r"khyber|kpk|peshawar"),
                      ("Punjab", r"punjab|lahore|rawalpindi|multan"),
                      ("Sindh", r"sindh|karachi"), ("Balochistan", r"balochistan|quetta")]:
        if re.search(pat, s):
            return name
    return "Other"


# ---- Adapters -------------------------------------------------------------
# Each returns a list of dicts with keys: id, title, agency, budget_text,
# published (YYYY-MM-DD), closing (YYYY-MM-DD), url, source.
# TODO: fill selectors after inspecting each portal's current HTML (DevTools).

def adapter_generic_table(name, url, row_sel, cols):
    """Generic HTML-table adapter. cols maps field -> column index."""
    def run():
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        out = []
        for tr in soup.select(row_sel):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if not cells:
                continue
            get = lambda k: cells[cols[k]] if k in cols and cols[k] < len(cells) else ""
            link = tr.find("a", href=True)
            out.append({
                "id": get("id") or (link.get_text(strip=True) if link else ""),
                "title": get("title"), "agency": get("agency"),
                "budget_text": get("budget"), "published": get("published"),
                "closing": get("closing"),
                "url": requests.compat.urljoin(url, link["href"]) if link else url,
                "source": name,
            })
        return out
    return run


SOURCES = [
    # Fill these in after checking each site's current structure and terms of use.
    # adapter_generic_table("PPRA Punjab", "https://<punjab-ppra-tenders-page>", "table tbody tr",
    #                       {"id":0,"title":1,"agency":2,"budget":3,"published":4,"closing":5}),
]


def normalise(rec):
    try:
        d_close = dt.datetime.strptime(rec["closing"][:10], "%Y-%m-%d").date()
    except Exception:
        log.debug("bad closing date: %s", rec.get("title"))
        return None
    if not KEYWORDS.search(rec["title"] or ""):
        return None  # not construction-related
    return {
        "id": rec["id"] or re.sub(r"\W+", "-", rec["title"])[:40],
        "title": rec["title"],
        "agency": rec["agency"] or "Unknown",
        "region": region_for(rec["source"], rec["title"] + " " + rec["agency"]),
        "category": categorise(rec["title"]),
        "budget": to_pkr_millions(rec["budget_text"]) or 0,
        "published": rec["published"][:10] or d_close.isoformat(),
        "closing": d_close.isoformat(),
        "source": rec["source"],
        "url": rec["url"],
    }


def main():
    rows, seen = [], set()
    for run in SOURCES:
        try:
            batch = run()
            log.info("%s: %d raw rows", run.__name__, len(batch))
        except Exception as e:  # keep going if one portal fails
            log.warning("source failed: %s", e)
            continue
        for rec in batch:
            n = normalise(rec)
            if n and (n["id"], n["source"]) not in seen:
                seen.add((n["id"], n["source"]))
                rows.append(n)
    if not rows:
        # Never overwrite the live data with an empty file.
        log.warning("no tenders collected; leaving %s unchanged", OUT)
        return
    OUT.write_text("const TENDERS = " + json.dumps(rows, indent=1, ensure_ascii=False) + ";\n",
                   encoding="utf-8")
    log.info("wrote %d tenders to %s", len(rows), OUT)


if __name__ == "__main__":
    main()
