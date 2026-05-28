"""
CanvasIQ Funding Directory — Daily Opportunity Scraper
=======================================================
Searches multiple sources daily for new startup grants, competitions,
and accelerator programs. Updates data.json automatically.

Sources:
  - RSS feeds: GrantsDatabase, OpportunityDesk, Grants.gov
  - Web search: DuckDuckGo HTML (no API key needed)

Run:  python scraper.py
Deps: pip install requests feedparser beautifulsoup4
"""

import json
import re
import time
import hashlib
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Optional

# ── try optional deps ────────────────────────────────────────────────────────
try:
    import feedparser
    HAS_FEEDPARSER = True
except ImportError:
    HAS_FEEDPARSER = False
    print("⚠  feedparser not installed — skipping RSS feeds. Run: pip install feedparser")

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False
    print("⚠  beautifulsoup4 not installed — skipping web search. Run: pip install beautifulsoup4")

# ── config ───────────────────────────────────────────────────────────────────

DATA_FILE = "data.json"

# RSS feeds to monitor daily
RSS_FEEDS = [
    "https://grantsdatabase.org/feed/",
    "https://opportunitydesk.org/feed/",
    "https://www.fundsforcompanies.fundsforngos.org/feed/",
    "https://grants.gov/rss/GG_NewOpp.xml",
]

# DuckDuckGo search queries to run daily
SEARCH_QUERIES = [
    "EdTech startup grant 2026 application open",
    "education startup pitch competition prize 2026",
    "startup competition grant NYC 2026",
    "AI startup grant competition 2026 apply",
    "women entrepreneur startup grant 2026",
    "BIPOC founder startup grant competition 2026",
    "pre-seed startup competition award 2026 virtual",
    "education technology accelerator program 2026",
    "startup pitch competition $100000 prize 2026",
    "small business grant competition technology 2026",
]

# Keywords that MUST appear for an entry to be considered relevant
RELEVANCE_KEYWORDS = [
    "grant", "competition", "award", "prize", "funding", "accelerator",
    "pitch", "challenge", "fellowship", "incubator", "startup", "entrepreneur"
]

# Keywords that make an entry higher confidence
QUALITY_KEYWORDS = [
    "apply", "application", "deadline", "open", "eligible", "winners",
    "cash", "equity-free", "non-dilutive", "no equity"
]

# Keywords to EXCLUDE (spam / irrelevant)
EXCLUDE_KEYWORDS = [
    "loan", "mortgage", "insurance", "casino", "gambling", "crypto",
    "nft", "forex", "trading signals", "diet", "weight loss"
]

# ── helpers ──────────────────────────────────────────────────────────────────

def load_data() -> dict:
    """Load existing data.json."""
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"lastUpdated": "", "totalFound": 0, "opportunities": []}

def save_data(data: dict):
    """Save updated data.json."""
    data["lastUpdated"] = datetime.now(timezone.utc).isoformat()
    data["totalFound"] = len(data["opportunities"])
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"✅ Saved {data['totalFound']} opportunities to {DATA_FILE}")

def make_id(url: str, title: str) -> str:
    """Create a stable unique ID from URL + title."""
    raw = (url + title).lower().strip()
    return int(hashlib.md5(raw.encode()).hexdigest()[:8], 16)

def existing_ids(data: dict) -> set:
    return {o["id"] for o in data["opportunities"]}

def existing_urls(data: dict) -> set:
    return {o["url"].lower().strip("/") for o in data["opportunities"]}

def existing_names(data: dict) -> set:
    return {o["name"].lower().strip() for o in data["opportunities"]}

def is_relevant(title: str, desc: str) -> bool:
    """Check if an entry is relevant to startup funding."""
    text = (title + " " + desc).lower()
    # Must have at least one relevance keyword
    if not any(kw in text for kw in RELEVANCE_KEYWORDS):
        return False
    # Must not contain exclusion keywords
    if any(kw in text for kw in EXCLUDE_KEYWORDS):
        return False
    return True

def confidence_score(title: str, desc: str) -> int:
    """Score 0–10 for quality of opportunity."""
    text = (title + " " + desc).lower()
    score = 0
    for kw in QUALITY_KEYWORDS:
        if kw in text:
            score += 1
    for kw in RELEVANCE_KEYWORDS:
        if kw in text:
            score += 1
    return min(score, 10)

# ── extraction ───────────────────────────────────────────────────────────────

def extract_prize(text: str) -> tuple:
    """Extract prize label and numeric value from text."""
    patterns = [
        (r"\$(\d+(?:\.\d+)?)\s*[Mm]illion", lambda m: (f"${m.group(1)}M", int(float(m.group(1)) * 1_000_000))),
        (r"\$(\d+(?:\.\d+)?)\s*[Mm]",        lambda m: (f"${m.group(1)}M", int(float(m.group(1)) * 1_000_000))),
        (r"\$(\d+(?:\.\d+)?)\s*[Kk]",        lambda m: (f"${m.group(1)}K", int(float(m.group(1)) * 1_000))),
        (r"\$(\d{1,3}(?:,\d{3})+)",           lambda m: (f"${m.group(1)}", int(m.group(1).replace(",", "")))),
        (r"\$(\d{3,})\b",                     lambda m: (f"${m.group(1)}", int(m.group(1)))),
        (r"€(\d+(?:\.\d+)?)\s*[Mm]",          lambda m: (f"€{m.group(1)}M", int(float(m.group(1)) * 1_000_000))),
        (r"€(\d+(?:\.\d+)?)\s*[Kk]",          lambda m: (f"€{m.group(1)}K", int(float(m.group(1)) * 1_000))),
    ]
    for pattern, extractor in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return extractor(m)
    return ("See listing", 0)

def extract_deadline(text: str) -> tuple:
    """Extract ISO deadline string and human-readable label."""
    month_map = {
        "january": "01", "february": "02", "march": "03", "april": "04",
        "may": "05", "june": "06", "july": "07", "august": "08",
        "september": "09", "october": "10", "november": "11", "december": "12",
        "jan": "01", "feb": "02", "mar": "03", "apr": "04",
        "jun": "06", "jul": "07", "aug": "08", "sep": "09",
        "oct": "10", "nov": "11", "dec": "12"
    }
    # Full date: "June 30, 2026" / "30 June 2026"
    patterns = [
        r"(January|February|March|April|May|June|July|August|September|October|November|December"
        r"|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
        r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(202[6-9])",

        r"(\d{1,2})(?:st|nd|rd|th)?\s+"
        r"(January|February|March|April|May|June|July|August|September|October|November|December"
        r"|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
        r"\s+(202[6-9])",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            groups = m.groups()
            if groups[0].isdigit():
                day, month_name, year = groups
            else:
                month_name, day, year = groups
            month_num = month_map.get(month_name.lower(), "01")
            day = str(day).zfill(2)
            iso = f"{year}-{month_num}-{day}"
            label = f"{month_name} {day}, {year}"
            # Only accept future dates
            try:
                if datetime.strptime(iso, "%Y-%m-%d") > datetime.now():
                    return iso, label
            except ValueError:
                pass
    return "rolling", "Rolling / Open"

def categorize(title: str, desc: str) -> dict:
    """Classify type, location, and sector from text."""
    text = (title + " " + desc).lower()

    # Type
    if any(w in text for w in ["grant", "funding opportunity", "award funds", "non-dilutive"]):
        opp_type = "grant"
    elif any(w in text for w in ["competition", "pitch", "contest", "challenge", "battlefield"]):
        opp_type = "competition"
    elif any(w in text for w in ["accelerator", "incubator", "cohort", "bootcamp"]):
        opp_type = "accelerator"
    elif any(w in text for w in ["corporate", "microsoft", "google", "amazon", "aws", "credits"]):
        opp_type = "corporate"
    elif any(w in text for w in ["award", "recognition", "prize"]):
        opp_type = "award"
    else:
        opp_type = "competition"

    # Location
    locs = []
    if any(w in text for w in ["new york", "nyc", "manhattan", "brooklyn", "queens", "bronx"]):
        locs.append("nyc")
    if any(w in text for w in ["national", "nationwide", "united states", "u.s.", "usa", "american", "u.s.-based"]):
        locs.append("national")
    if any(w in text for w in ["virtual", "online", "remote", "digital", "web-based"]):
        locs.append("virtual")
    if any(w in text for w in ["global", "international", "worldwide", "world", "country"]):
        locs.append("global")
    if not locs:
        locs = ["national"]

    # Sector
    sectors = []
    if any(w in text for w in ["edtech", "education technology", "learning", "educational",
                                 "classroom", "k-12", "k12", "higher ed", "university", "school"]):
        sectors.append("edtech")
    if any(w in text for w in ["artificial intelligence", "machine learning", "deep learning",
                                " ai ", "ai-", "-ai", "llm", "generative"]):
        sectors.append("ai")
    if any(w in text for w in ["women", "female", "woman-owned", "women-led", "women-founded"]):
        sectors.append("women")
    if any(w in text for w in ["black", "bipoc", "minority", "diverse founder",
                                 "underrepresented", "hispanic", "latina", "people of color"]):
        sectors.append("bipoc")
    if not sectors:
        sectors = ["general"]

    equity_indicators = ["equity", "stake", "ownership", "convertible note", "series"]
    equity = any(w in text for w in equity_indicators)

    return {"type": opp_type, "loc": locs, "sector": sectors, "equity": equity}

# ── RSS scraping ─────────────────────────────────────────────────────────────

def scrape_rss(url: str) -> list:
    """Fetch and parse an RSS feed, returning raw entries."""
    if not HAS_FEEDPARSER:
        return []
    try:
        print(f"  📡 RSS: {url}")
        feed = feedparser.parse(url)
        results = []
        for entry in feed.entries[:30]:  # latest 30 entries
            title = getattr(entry, "title", "")
            summary = getattr(entry, "summary", "") or getattr(entry, "description", "")
            link = getattr(entry, "link", "")
            # Strip HTML from summary
            if HAS_BS4:
                summary = BeautifulSoup(summary, "html.parser").get_text(" ", strip=True)
            else:
                summary = re.sub(r"<[^>]+>", " ", summary)
            results.append({"title": title, "desc": summary[:400], "url": link})
        return results
    except Exception as e:
        print(f"  ⚠  RSS error ({url}): {e}")
        return []

# ── web search (DuckDuckGo HTML, no API key) ─────────────────────────────────

def search_duckduckgo(query: str) -> list:
    """Search DuckDuckGo and return list of {title, desc, url}."""
    if not HAS_BS4:
        return []
    try:
        encoded = urllib.parse.quote_plus(query)
        url = f"https://html.duckduckgo.com/html/?q={encoded}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        soup = BeautifulSoup(html, "html.parser")
        results = []
        for item in soup.select(".result"):
            t_el = item.select_one(".result__title")
            s_el = item.select_one(".result__snippet")
            u_el = item.select_one(".result__url")
            if not t_el:
                continue
            title = t_el.get_text(" ", strip=True)
            desc = s_el.get_text(" ", strip=True) if s_el else ""
            link = ""
            a = t_el.find("a")
            if a and a.get("href"):
                href = a["href"]
                # DuckDuckGo wraps URLs — extract uddg= param
                parsed = urllib.parse.urlparse(href)
                qs = urllib.parse.parse_qs(parsed.query)
                link = qs.get("uddg", [href])[0]
            elif u_el:
                link = "https://" + u_el.get_text(strip=True)
            results.append({"title": title, "desc": desc[:400], "url": link})
        return results[:8]  # top 8 per query
    except Exception as e:
        print(f"  ⚠  Search error ('{query[:40]}…'): {e}")
        return []

# ── main pipeline ─────────────────────────────────────────────────────────────

def build_opportunity(raw: dict, existing_data: dict) -> Optional[dict]:
    """
    Turn a raw {title, desc, url} dict into a structured opportunity,
    or return None if it's irrelevant / already exists.
    """
    title = raw.get("title", "").strip()
    desc = raw.get("desc", "").strip()
    url = raw.get("url", "").strip()

    if not title or not url:
        return None

    # Skip if already tracked
    urls = existing_urls(existing_data)
    names = existing_names(existing_data)
    clean_url = url.lower().strip("/")
    if clean_url in urls:
        return None
    if title.lower().strip() in names:
        return None

    # Relevance check
    if not is_relevant(title, desc):
        return None

    # Confidence threshold (must score ≥ 2)
    if confidence_score(title, desc) < 2:
        return None

    cat = categorize(title, desc)
    prize_label, prize_val = extract_prize(title + " " + desc)
    deadline, deadline_label = extract_deadline(title + " " + desc)

    opp_id = make_id(url, title)
    # Make sure ID doesn't collide
    existing = existing_ids(existing_data)
    while opp_id in existing:
        opp_id += 1

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    return {
        "id": opp_id,
        "name": title[:120],
        "desc": desc[:500],
        "prize": prize_label,
        "prizeVal": prize_val,
        "deadline": deadline,
        "deadlineLabel": deadline_label,
        "type": cat["type"],
        "loc": cat["loc"],
        "sector": cat["sector"],
        "url": url,
        "equity": cat["equity"],
        "addedDate": today,
        "source": "auto"
    }

def run():
    print("🤖 CanvasIQ Funding Scraper — starting daily update")
    print(f"   {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}\n")

    data = load_data()
    initial_count = len(data["opportunities"])
    new_count = 0

    # ── 1. RSS feeds ──────────────────────────────────────────────────────────
    print("── RSS Feeds ──────────────────────────────────────────────────────")
    for feed_url in RSS_FEEDS:
        entries = scrape_rss(feed_url)
        for raw in entries:
            opp = build_opportunity(raw, data)
            if opp:
                data["opportunities"].append(opp)
                new_count += 1
                print(f"  ✨ NEW [{opp['type'].upper()}]: {opp['name'][:70]}")
        time.sleep(1)  # polite delay

    # ── 2. Web searches ───────────────────────────────────────────────────────
    print("\n── Web Searches ───────────────────────────────────────────────────")
    for query in SEARCH_QUERIES:
        print(f"  🔍 '{query}'")
        results = search_duckduckgo(query)
        for raw in results:
            opp = build_opportunity(raw, data)
            if opp:
                data["opportunities"].append(opp)
                new_count += 1
                print(f"  ✨ NEW [{opp['type'].upper()}]: {opp['name'][:70]}")
        time.sleep(2)  # respectful delay between searches

    # ── 3. Remove expired (past deadline by >30 days) ────────────────────────
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    before = len(data["opportunities"])
    data["opportunities"] = [
        o for o in data["opportunities"]
        if o["deadline"] == "rolling" or o["deadline"] >= cutoff
    ]
    removed = before - len(data["opportunities"])
    if removed:
        print(f"\n🗑  Removed {removed} expired opportunities (>30 days past deadline)")

    # ── 4. Sort by deadline (rolling last) ───────────────────────────────────
    def sort_key(o):
        d = o["deadline"]
        return "9999-99-99" if d == "rolling" else d

    data["opportunities"].sort(key=sort_key)

    # ── summary ───────────────────────────────────────────────────────────────
    print(f"\n── Summary ────────────────────────────────────────────────────────")
    print(f"   Before : {initial_count} opportunities")
    print(f"   Added  : {new_count} new")
    print(f"   Removed: {removed} expired")
    print(f"   Total  : {len(data['opportunities'])} opportunities")

    save_data(data)
    print("\n✅ Done! data.json updated.")

if __name__ == "__main__":
    run()
