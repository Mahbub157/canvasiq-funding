"""
CanvasIQ Funding Directory — Unified Deep Opportunity Engine
============================================================
One engine that discovers startup/student grants, competitions, accelerators,
awards, fellowships and scholarships from MANY sources every day, then writes
BOTH output files the site needs:

  • live-events.json  → the file funding.html actually fetches (page schema)
  • data.json         → the richer archive (curated + auto, internal schema)

Sources (all best-effort; any that fail are skipped, never fatal):
  • Live JSON APIs : Devpost, Challenge.gov, HeroX
  • RSS feeds      : OpportunityDesk, Grants.gov, FundsForCompanies, GrantsDatabase
  • Deep web search: DuckDuckGo HTML (no API key) across many tuned queries

Run:   python scraper.py
Deps:  pip install -r requirements.txt   (feedparser + bs4 optional; live APIs
       and JSON IO work on the Python standard library alone)
"""

import json
import re
import time
import html
import hashlib
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Optional

# ── optional deps (graceful) ──────────────────────────────────────────────────
try:
    import feedparser
    HAS_FEEDPARSER = True
except ImportError:
    HAS_FEEDPARSER = False
    print("⚠  feedparser not installed — RSS feeds skipped. (pip install feedparser)")

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False
    print("⚠  beautifulsoup4 not installed — web search skipped. (pip install beautifulsoup4)")

# ── config ────────────────────────────────────────────────────────────────────

DATA_FILE = "data.json"            # rich internal archive
LIVE_FILE = "live-events.json"     # consumed directly by funding.html
EXPIRE_AFTER_DAYS = 7              # drop entries this many days past deadline
MAX_LIVE = 120                     # cap entries written to live-events.json

# Only these sources return REAL, structured deadlines straight from an official
# API, so only they are trusted enough to publish to the live site. RSS + web
# search are kept for discovery but never published unverified (they're the
# source of wrong dates / non-existent events).
VERIFIED_SOURCES = {"devpost", "challengegov", "herox", "grantsgov"}
PUBLISH_UNVERIFIED = False         # keep False so nothing unverified hits the site

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Live JSON APIs — (name, url, parser_key)
DEVPOST_URL = "https://devpost.com/api/hackathons?status[]=upcoming&status[]=open&order_by=deadline&per_page=48"
CHALLENGEGOV_URL = "https://api.challenge.gov/api/challenges?status=active&limit=40"
HEROX_URL = "https://www.herox.com/api/v1/challenges?status=active&page_size=30"
GRANTSGOV_URL = "https://api.grants.gov/v1/api/search2"

RSS_FEEDS = [
    "https://opportunitydesk.org/feed/",
    "https://grants.gov/rss/GG_NewOpp.xml",
    "https://www.fundsforcompanies.fundsforngos.org/feed/",
    "https://grantsdatabase.org/feed/",
]

# Deep, tuned search queries — broad coverage so nothing is missed.
SEARCH_QUERIES = [
    "startup grant 2026 application open non-dilutive",
    "startup pitch competition 2026 cash prize apply",
    "startup accelerator program 2026 application open",
    "AI startup grant competition 2026 apply",
    "EdTech startup grant competition 2026",
    "women entrepreneur startup grant 2026 apply",
    "BIPOC minority founder startup grant 2026",
    "student innovation competition 2026 prize apply",
    "fellowship for entrepreneurs 2026 application",
    "social impact startup award 2026 apply",
    "climate tech startup grant competition 2026",
    "fintech startup competition 2026 prize",
    "Bangladesh startup competition grant 2026 apply",
    "international scholarship 2026 fully funded apply",
    "hackathon 2026 prize registration open",
    "pre-seed startup competition 2026 equity-free",
]

RELEVANCE_KEYWORDS = [
    "grant", "competition", "award", "prize", "funding", "accelerator",
    "pitch", "challenge", "fellowship", "incubator", "startup", "entrepreneur",
    "scholarship", "hackathon", "innovation",
]
QUALITY_KEYWORDS = [
    "apply", "application", "deadline", "open", "eligible", "winners",
    "cash", "equity-free", "non-dilutive", "no equity", "register", "submit",
]
EXCLUDE_KEYWORDS = [
    "loan", "mortgage", "insurance", "casino", "gambling", "betting",
    "nft", "forex", "trading signals", "diet", "weight loss", "porn",
    "essay writing service", "write my essay",
]

# ── IO ──────────────────────────────────────────────────────────────────────

def load_json(path: str, default: dict) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def fetch_json(url: str, timeout: int = 15) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="ignore"))
    except Exception as e:
        print(f"  ⚠  API error ({url[:50]}…): {e}")
        return None


def fetch_html(url: str, timeout: int = 12) -> Optional[str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"  ⚠  fetch error ({url[:50]}…): {e}")
        return None


# ── classification / extraction ───────────────────────────────────────────────

def clean(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def is_relevant(title: str, desc: str) -> bool:
    t = (title + " " + desc).lower()
    if not any(k in t for k in RELEVANCE_KEYWORDS):
        return False
    if any(k in t for k in EXCLUDE_KEYWORDS):
        return False
    return True


def confidence(title: str, desc: str) -> int:
    t = (title + " " + desc).lower()
    return min(sum(k in t for k in QUALITY_KEYWORDS) + sum(k in t for k in RELEVANCE_KEYWORDS), 12)


def detect_type(title: str, desc: str) -> str:
    """Return capitalized type used by the site UI."""
    t = (title + " " + desc).lower()
    if any(w in t for w in ["scholarship", "fully funded", "tuition"]):
        return "Scholarship"
    if any(w in t for w in ["fellowship", "fellow "]):
        return "Fellowship"
    if any(w in t for w in ["grant", "non-dilutive", "funding opportunity"]):
        return "Grant"
    if any(w in t for w in ["accelerator", "incubator", "cohort", "bootcamp"]):
        return "Competition"
    if any(w in t for w in ["competition", "pitch", "contest", "challenge", "hackathon", "battlefield", "cup"]):
        return "Competition"
    if any(w in t for w in ["award", "prize", "recognition"]):
        return "Prize"
    if any(w in t for w in ["summit", "conference", "forum", "expo", "festival", "meetup"]):
        return "Event"
    return "Competition"


def detect_country(title: str, desc: str, default: str = "Global") -> str:
    """Return one of: 'Bangladesh', 'United States', 'Global'."""
    t = (title + " " + desc).lower()
    if any(w in t for w in ["bangladesh", "dhaka", "bdt", "৳", "bangladeshi"]):
        return "Bangladesh"
    if any(w in t for w in ["united states", "u.s.", " usa", "american", "nyc", "new york", "sba", "sbir", "challenge.gov"]):
        return "United States"
    return default


def detect_sector(title: str, desc: str) -> list:
    t = (title + " " + desc).lower()
    s = []
    if any(w in t for w in ["edtech", "education", "learning", "classroom", "k-12", "k12", "school", "university", "student"]):
        s.append("edtech")
    if any(w in t for w in ["artificial intelligence", "machine learning", " ai ", "ai-", "llm", "generative"]):
        s.append("ai")
    if any(w in t for w in ["women", "female", "woman-owned", "women-led"]):
        s.append("women")
    if any(w in t for w in ["black", "bipoc", "minority", "underrepresented", "latina", "hispanic"]):
        s.append("bipoc")
    if any(w in t for w in ["climate", "sustainab", "green", "clean energy", "carbon"]):
        s.append("climate")
    if any(w in t for w in ["fintech", "financial", "payments", "banking"]):
        s.append("fintech")
    return s or ["general"]


MONTHS = {m: f"{i:02d}" for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}
MONTHS.update({m[:3]: n for m, n in list(MONTHS.items())})


def extract_deadline(text: str) -> Optional[str]:
    """Return ISO date string for a future deadline, or None (rolling)."""
    pats = [
        r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(202[6-9])",
        r"(\d{1,2})(?:st|nd|rd|th)?\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(202[6-9])",
    ]
    for pat in pats:
        m = re.search(pat, text, re.IGNORECASE)
        if not m:
            continue
        g = m.groups()
        if g[0].isdigit():
            day, mon, year = g
        else:
            mon, day, year = g
        mn = MONTHS.get(mon.lower()[:3])
        if not mn:
            continue
        iso = f"{year}-{mn}-{int(day):02d}"
        try:
            if datetime.strptime(iso, "%Y-%m-%d").date() >= datetime.now(timezone.utc).date():
                return iso
        except ValueError:
            pass
    return None


def extract_amount(text: str) -> str:
    m = re.search(r"\$\s?\d[\d,]*(?:\.\d+)?\s*(?:million|m|k|thousand)?", text, re.IGNORECASE)
    if m:
        return m.group(0).strip()
    m = re.search(r"€\s?\d[\d,]*(?:\.\d+)?\s*(?:million|m|k)?", text, re.IGNORECASE)
    return m.group(0).strip() if m else "See listing"


def make_id(url: str, title: str) -> int:
    raw = (url + "|" + title).lower().strip()
    return 6000 + int(hashlib.md5(raw.encode()).hexdigest()[:6], 16) % 900000


# ── normalized record ──────────────────────────────────────────────────────────

def make_record(title, desc, url, *, org="Online", amt=None, deadline=None,
                country=None, img="", source="auto", opp_type=None) -> Optional[dict]:
    title = clean(title)[:140]
    desc = clean(desc)[:480]
    url = (url or "").strip()
    if not title or not url or not url.startswith("http"):
        return None
    blob = title + " " + desc
    opp_type = opp_type or detect_type(title, desc)
    country = country or detect_country(title, desc)
    deadline = deadline or extract_deadline(blob)
    amt = amt or extract_amount(blob)
    if not desc:
        desc = f"{opp_type} opportunity — open for applications. See the official listing for full eligibility."
    return {
        "id": make_id(url, title),
        "title": title,
        "desc": desc,
        "type": opp_type,
        "country": country,
        "org": clean(org)[:80] or "Online",
        "amt": amt,
        "deadline": deadline,             # ISO or None (rolling)
        "url": url,
        "img": img or "",
        "sector": detect_sector(title, desc),
        "source": source,
        "addedDate": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


# ── live API sources ────────────────────────────────────────────────────────────

def from_devpost() -> list:
    out = []
    data = fetch_json(DEVPOST_URL)
    if not data:
        return out
    for h in data.get("hackathons", []):
        dates = h.get("submission_period_dates", "") or ""
        m = re.search(r"[-–]\s*([A-Za-z]+ \d+,?\s*\d{4})", dates)
        dl = extract_deadline(m.group(1)) if m else None
        prize = h.get("prize_amount") or 0
        try:
            prize_n = int(re.sub(r"[^\d]", "", str(prize)) or 0)
        except ValueError:
            prize_n = 0
        rec = make_record(
            h.get("title", ""), h.get("tagline", "") or "Global hackathon open worldwide.",
            h.get("url", ""), org=h.get("organization_name") or "Devpost",
            amt=f"${prize_n:,} in prizes" if prize_n else "Open prizes",
            deadline=dl, country="Global", img=h.get("thumbnail_url", ""),
            source="devpost", opp_type="Hackathon")
        if rec:
            out.append(rec)
    print(f"  ✓ Devpost: {len(out)}")
    return out


def from_challengegov() -> list:
    out = []
    data = fetch_json(CHALLENGEGOV_URL)
    if not data:
        return out
    rows = (data.get("_embedded", {}) or {}).get("results", []) or data.get("data", []) or []
    for c in rows:
        dl_raw = c.get("submission_end_date") or c.get("end_date") or ""
        dl = dl_raw[:10] if dl_raw else None
        prize = c.get("total_prize_offered_amount") or 0
        try:
            prize_n = int(float(prize))
        except (ValueError, TypeError):
            prize_n = 0
        rec = make_record(
            c.get("title", ""), c.get("brief_description") or c.get("description") or "",
            c.get("external_url") or "https://challenge.gov",
            org=c.get("agency_name") or "US Government",
            amt=f"${prize_n:,}" if prize_n else "Government prize",
            deadline=dl, country="United States", source="challengegov", opp_type="Competition")
        if rec:
            out.append(rec)
    print(f"  ✓ Challenge.gov: {len(out)}")
    return out


def from_herox() -> list:
    out = []
    data = fetch_json(HEROX_URL)
    if not data:
        return out
    for h in (data.get("results") or data.get("data") or []):
        dl_raw = h.get("end_date") or h.get("submission_deadline") or ""
        dl = dl_raw[:10] if dl_raw else None
        prize = h.get("prize_amount") or h.get("total_prize") or 0
        try:
            prize_n = int(float(prize))
        except (ValueError, TypeError):
            prize_n = 0
        url = h.get("url", "") or ""
        if url.startswith("/"):
            url = "https://herox.com" + url
        rec = make_record(
            h.get("title") or h.get("name", ""), h.get("summary") or h.get("tagline") or "",
            url or "https://herox.com",
            org=(h.get("organization") or {}).get("name") if isinstance(h.get("organization"), dict) else "HeroX",
            amt=f"${prize_n:,}" if prize_n else "Open prize",
            deadline=dl, country="Global", img=h.get("image_url", ""),
            source="herox", opp_type="Competition")
        if rec:
            out.append(rec)
    print(f"  ✓ HeroX: {len(out)}")
    return out


def from_grantsgov() -> list:
    """US federal grants from the official Grants.gov search2 API (real close dates)."""
    out = []
    try:
        body = json.dumps({
            "rows": 80,
            "keyword": "startup OR innovation OR technology OR education OR research OR small business",
            "oppStatuses": "posted|forecasted",
        }).encode("utf-8")
        req = urllib.request.Request(GRANTSGOV_URL, data=body,
                                     headers={"User-Agent": UA, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", errors="ignore"))
    except Exception as e:
        print(f"  ⚠  Grants.gov error: {e}")
        return out
    hits = (data.get("data") or {}).get("oppHits") or []
    for h in hits:
        iso = None
        raw = h.get("closeDate") or ""
        if raw:
            for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
                try:
                    iso = datetime.strptime(raw, fmt).strftime("%Y-%m-%d"); break
                except ValueError:
                    pass
        opp_id = h.get("id") or ""
        rec = make_record(
            h.get("title", ""),
            f"US federal grant from {h.get('agencyCode', 'a federal agency')}. Opportunity #{h.get('number', '')}.",
            f"https://www.grants.gov/search-results-detail/{opp_id}" if opp_id else "https://www.grants.gov",
            org=h.get("agency") or h.get("agencyCode") or "Grants.gov",
            amt="Federal grant", deadline=iso, country="United States",
            source="grantsgov", opp_type="Grant")
        if rec:
            out.append(rec)
    print(f"  ✓ Grants.gov: {len(out)}")
    return out


# ── RSS + web search ────────────────────────────────────────────────────────────

def from_rss() -> list:
    out = []
    if not HAS_FEEDPARSER:
        return out
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            n = 0
            for e in feed.entries[:30]:
                title = getattr(e, "title", "")
                summary = getattr(e, "summary", "") or getattr(e, "description", "")
                link = getattr(e, "link", "")
                if not is_relevant(title, summary) or confidence(title, summary) < 2:
                    continue
                rec = make_record(title, summary, link, org="OpportunityDesk", source="rss")
                if rec:
                    out.append(rec); n += 1
            print(f"  ✓ RSS {url.split('/')[2]}: {n}")
        except Exception as ex:
            print(f"  ⚠  RSS {url}: {ex}")
        time.sleep(1)
    return out


def from_search() -> list:
    out = []
    if not HAS_BS4:
        return out
    for q in SEARCH_QUERIES:
        html_doc = fetch_html(f"https://html.duckduckgo.com/html/?q={urllib.parse.quote_plus(q)}")
        if not html_doc:
            time.sleep(2); continue
        soup = BeautifulSoup(html_doc, "html.parser")
        n = 0
        for item in soup.select(".result")[:10]:
            t_el = item.select_one(".result__title")
            s_el = item.select_one(".result__snippet")
            if not t_el:
                continue
            title = t_el.get_text(" ", strip=True)
            desc = s_el.get_text(" ", strip=True) if s_el else ""
            link = ""
            a = t_el.find("a")
            if a and a.get("href"):
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(a["href"]).query)
                link = qs.get("uddg", [a["href"]])[0]
            if not is_relevant(title, desc) or confidence(title, desc) < 3:
                continue
            rec = make_record(title, desc, link, source="search")
            if rec:
                out.append(rec); n += 1
        print(f"  ✓ search '{q[:32]}…': {n}")
        time.sleep(2)
    return out


# ── output serializers ──────────────────────────────────────────────────────────

def to_live_entry(r: dict) -> dict:
    """Shape exactly as funding.html's fetchLiveData() expects."""
    return {
        "title": r["title"],
        "type": r["type"],
        "country": r["country"],
        "org": r["org"],
        "amt": r["amt"],
        "deadline": r["deadline"],          # ISO or null → site treats as rolling
        "elig": "Open — see official listing",
        "link": r["url"],
        "desc": r["desc"],
        "img": r["img"],
        "source": r["source"],
        "verified": r.get("verified", False),
        "checkedAt": r.get("checkedAt", ""),
        "liveAPI": True,
    }


def to_data_entry(r: dict) -> dict:
    """Richer internal archive schema (data.json)."""
    loc = {"Bangladesh": ["bangladesh"], "United States": ["national"]}.get(r["country"], ["global"])
    return {
        "id": r["id"],
        "name": r["title"],
        "desc": r["desc"],
        "prize": r["amt"],
        "deadline": r["deadline"] or "rolling",
        "deadlineLabel": r["deadline"] or "Rolling / Open",
        "type": r["type"].lower(),
        "loc": loc,
        "sector": r["sector"],
        "url": r["url"],
        "country": r["country"],
        "addedDate": r["addedDate"],
        "source": r["source"],
        "verified": r.get("verified", False),
        "checkedAt": r.get("checkedAt", ""),
    }


# ── pipeline ────────────────────────────────────────────────────────────────────

def dedupe(records: list) -> list:
    seen_url, seen_title, out = set(), set(), []
    for r in records:
        u = r["url"].lower().rstrip("/")
        t = r["title"].lower().strip()[:50]
        if u in seen_url or t in seen_title:
            continue
        seen_url.add(u); seen_title.add(t); out.append(r)
    return out


def not_expired(deadline: Optional[str], cutoff: str) -> bool:
    if not deadline or deadline == "rolling":
        return True
    return deadline >= cutoff


def run():
    started = datetime.now(timezone.utc)
    print(f"🤖 CanvasIQ Unified Scraper — {started:%Y-%m-%d %H:%M UTC}\n")

    sources_ok, records = [], []

    print("── Live APIs ──")
    for name, fn in [("Devpost", from_devpost), ("Challenge.gov", from_challengegov), ("HeroX", from_herox), ("Grants.gov", from_grantsgov)]:
        try:
            got = fn(); records += got; sources_ok.append(f"{name}: {len(got)}")
        except Exception as e:
            sources_ok.append(f"{name}: failed ({e})")
        time.sleep(1)

    print("\n── RSS Feeds ──")
    try:
        got = from_rss(); records += got; sources_ok.append(f"RSS: {len(got)}")
    except Exception as e:
        sources_ok.append(f"RSS: failed ({e})")

    print("\n── Deep Web Search ──")
    try:
        got = from_search(); records += got; sources_ok.append(f"Search: {len(got)}")
    except Exception as e:
        sources_ok.append(f"Search: failed ({e})")

    # Dedupe + drop expired
    cutoff = (started - timedelta(days=EXPIRE_AFTER_DAYS)).strftime("%Y-%m-%d")
    records = [r for r in dedupe(records) if not_expired(r["deadline"], cutoff)]

    # Tag verification + the date we checked it, so the site can show trust.
    today = started.strftime("%Y-%m-%d")
    for r in records:
        r["verified"] = r["source"] in VERIFIED_SOURCES
        r["checkedAt"] = today

    # Sort: soonest real deadline first, rolling last
    records.sort(key=lambda r: r["deadline"] or "9999-99-99")

    # ── write live-events.json (what the site reads) ──────────────────────────
    # Publish ONLY verified entries (real deadlines from official APIs) unless
    # PUBLISH_UNVERIFIED is explicitly turned on.
    publishable = [r for r in records if r["verified"] or PUBLISH_UNVERIFIED]
    new_live = [to_live_entry(r) for r in publishable]

    # PERSISTENCE: don't wipe what we found before. Keep every previously-saved
    # event that is still verified and not past its deadline (i.e. running or
    # upcoming), then add today's newly discovered verified events on top.
    prev = load_json(LIVE_FILE, {"opportunities": []}).get("opportunities", [])
    prev_keep = [o for o in prev
                 if o.get("verified") and not_expired(o.get("deadline"), cutoff)]

    merged_live, seen_u, seen_t = [], set(), set()
    for o in prev_keep + new_live:           # previous first, so running events stick
        u = (o.get("link", "") or "").lower().rstrip("/")
        t = (o.get("title", "") or "").lower().strip()[:50]
        if (u and u in seen_u) or (t and t in seen_t):
            continue
        seen_u.add(u); seen_t.add(t); merged_live.append(o)

    merged_live.sort(key=lambda o: o.get("deadline") or "9999-99-99")
    live = merged_live[:MAX_LIVE]
    kept = len(prev_keep); added = len(live) - sum(1 for o in live if o in prev_keep)
    live_out = {
        "updated": started.isoformat(),
        "count": len(live),
        "verifiedCount": len(live),
        "policy": "verified-only · persistent" if not PUBLISH_UNVERIFIED else "includes-unverified",
        "sources": sources_ok,
        "opportunities": live,
    }
    print(f"   kept {kept} prior verified · merged to {len(live)} total")
    with open(LIVE_FILE, "w", encoding="utf-8") as f:
        json.dump(live_out, f, indent=2, ensure_ascii=False)
    print(f"\n💾 {LIVE_FILE}: {len(live)} live opportunities")

    # ── merge into data.json archive (preserve curated 'manual' entries) ──────
    archive = load_json(DATA_FILE, {"opportunities": []})
    manual = [o for o in archive.get("opportunities", []) if o.get("source") == "manual"]
    manual = [o for o in manual if not_expired(o.get("deadline"), cutoff)]
    manual_urls = {o.get("url", "").lower().rstrip("/") for o in manual}
    auto = [to_data_entry(r) for r in records if r["url"].lower().rstrip("/") not in manual_urls]
    merged = manual + auto
    merged.sort(key=lambda o: o.get("deadline") if o.get("deadline") not in (None, "rolling") else "9999-99-99")
    data_out = {
        "lastUpdated": started.isoformat(),
        "totalFound": len(merged),
        "opportunities": merged,
    }
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data_out, f, indent=2, ensure_ascii=False)
    print(f"💾 {DATA_FILE}: {len(merged)} total ({len(manual)} curated + {len(auto)} auto)")

    print("\n── Sources ──")
    for s in sources_ok:
        print(f"   {s}")
    print(f"\n✅ Done in {(datetime.now(timezone.utc) - started).seconds}s")


if __name__ == "__main__":
    run()
