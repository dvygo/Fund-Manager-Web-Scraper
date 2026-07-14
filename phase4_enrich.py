"""MF-Engine — Phase 4: enrich fund managers with LinkedIn + email.

Reads data/fund_managers.csv (Phase 3) and adds, per manager:
  - linkedin_url : profile URL discovered via a Bing search (stored, never
                   scraped — LinkedIn hard-walls bots and hides emails anyway)
  - email        : a *verified* address only — kept from the AMC page if
                   Phase 3 found one, else Hunter.io (if HUNTER_API_KEY set),
                   else SMTP-verified (if VERIFY_SMTP=1)
  - email_guess  : best-effort corporate pattern (first.last@domain). Clearly
                   separate from `email` — a guess, not asserted fact.

Search: Brave Search API when BRAVE_API_KEY is set (keyed, reliable, free tier
~2000/mo — covers a full roster with no throttling); otherwise SearXNG (self-
hosted, no key, but its engine scrapes get IP-throttled/Tor-blocked so coverage
is flaky). The discovered profile URL is name-matched to the manager and stored;
no LinkedIn page is ever fetched.

Verified profiles in linkedin_overrides.json (keyed "name|firm") are applied as
authoritative and skip search.

Output: data/fund_managers_enriched.csv

Env:
  BRAVE_API_KEY    Brave Search API token (recommended — reliable, free tier)
  SEARXNG_URL      SearXNG instance URL (fallback; default http://localhost:8080)
  HUNTER_API_KEY   use Hunter.io email-finder for verified emails
  VERIFY_SMTP=1    attempt SMTP RCPT verification of guessed emails (slow,
                   often blocked by corporate mail servers; needs dnspython)
  MAX_MANAGERS=N   cap rows processed (testing)

Usage:
    python phase4_enrich.py
"""

import asyncio
import csv
import json
import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mf-engine.enrich")
logging.getLogger("httpx").setLevel(logging.WARNING)

INPUT_CSV = Path("data/fund_managers.csv")
OUTPUT_CSV = Path("data/fund_managers_enriched.csv")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
LINKEDIN_RE = re.compile(
    r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9\-_%]+", re.IGNORECASE
)

BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:8080").rstrip("/")
HUNTER_API_KEY = os.environ.get("HUNTER_API_KEY", "")
VERIFY_SMTP = os.environ.get("VERIFY_SMTP") == "1"
MAX_MANAGERS = int(os.environ.get("MAX_MANAGERS", "0"))
OVERRIDES_PATH = Path("linkedin_overrides.json")
# Gap between live SearXNG queries — keeps upstream engines from throttling.
SEARCH_GAP_SECONDS = float(os.environ.get("SEARCH_GAP_SECONDS", "4"))


def name_parts(full: str) -> tuple[str, str]:
    """First and last token of a name, initials/honorifics dropped."""
    tokens = [t.strip(".") for t in full.split() if t.strip(".")]
    tokens = [t for t in tokens if t.lower() not in {"mr", "ms", "mrs", "dr"}]
    words = [t for t in tokens if len(t) > 1] or tokens
    if not words:
        return "", ""
    return words[0].lower(), words[-1].lower()


def domain_of(source_url: str) -> str:
    return urlparse(source_url).netloc.lower().removeprefix("www.")


def email_candidates(first: str, last: str, domain: str) -> list[str]:
    """Common corporate email patterns, most-likely first."""
    if not (first and last and domain):
        return []
    f, l = first, last
    return [
        f"{f}.{l}@{domain}",
        f"{f}{l}@{domain}",
        f"{f[0]}{l}@{domain}",
        f"{f}_{l}@{domain}",
        f"{f}@{domain}",
        f"{l}.{f}@{domain}",
    ]


def firm_for_query(firm: str) -> str:
    """Trim 'Mutual Fund' — managers' profiles name the AMC/asset arm, not the
    fund brand ('360 ONE Asset', not '360 ONE Mutual Fund')."""
    return re.sub(r"\s*mutual\s+fund\s*$", "", firm, flags=re.IGNORECASE).strip()


def _name_matches(url: str, title: str, name: str) -> bool:
    """Guard against grabbing the wrong person's profile: the manager's last
    name (and ideally first) must appear in the profile slug or result title.
    Stops e.g. three different 'Dhaval's collapsing to one 'dhavalsays' URL."""
    slug = urlparse(url).path.lower()
    hay = f"{slug} {title.lower()}"
    parts = [p for p in re.split(r"\s+", name.lower()) if len(p) > 1]
    if not parts:
        return False
    first, last = parts[0], parts[-1]
    if last not in hay:
        return False
    # single-token slug like /in/dhavalsays must also carry the first name
    return first in hay or first[:4] in slug


def _pick_profile(candidates: list[tuple[str, str]], name: str) -> str:
    """Best linkedin.com/in URL from (url, title) pairs, name-matched."""
    for url, title in candidates:
        hits = LINKEDIN_RE.findall(url)
        if hits and _name_matches(hits[0], title, name):
            return hits[0].split("?")[0]
    return ""


async def brave_search(client: httpx.AsyncClient, query: str) -> list[tuple[str, str]]:
    """(url, title) pairs from the Brave Search API. Keyed, no per-run
    throttling within the free quota — reliable for a full roster."""
    resp = await client.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": 10},
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": BRAVE_API_KEY,
        },
    )
    resp.raise_for_status()
    results = resp.json().get("web", {}).get("results", [])
    return [(r.get("url", ""), r.get("title", "")) for r in results]


async def searxng_search(client: httpx.AsyncClient, query: str) -> list[tuple[str, str]]:
    """(url, title) pairs from SearXNG's JSON API."""
    resp = await client.get(
        f"{SEARXNG_URL}/search",
        params={
            "q": query,
            "format": "json",
            # Tor-tolerant engines (Google/Bing block Tor exits)
            "engines": "duckduckgo,startpage,brave,mojeek,qwant",
        },
    )
    return [
        (i.get("url", ""), i.get("title", ""))
        for i in resp.json().get("results", [])
    ]


async def find_linkedin(client: httpx.AsyncClient, name: str, firm: str) -> str:
    """Name-matched linkedin.com/in URL. Brave Search API when BRAVE_API_KEY is
    set (reliable), else SearXNG. Stored only, never scraped. One retry, since a
    busy engine can return empty briefly."""
    query = f"{name} {firm_for_query(firm)} fund manager linkedin"
    search = brave_search if BRAVE_API_KEY else searxng_search
    for attempt in range(2):
        try:
            hit = _pick_profile(await search(client, query), name)
            if hit or attempt == 1:
                return hit
        except Exception:
            log.debug("search failed: %s", query)
            if attempt == 1:
                return ""
        await asyncio.sleep(1.5)
    return ""


async def hunter_email(
    client: httpx.AsyncClient, first: str, last: str, domain: str
) -> tuple[str, str]:
    """(email, confidence) from Hunter.io, or ('', '') if unavailable."""
    if not (HUNTER_API_KEY and first and last and domain):
        return "", ""
    try:
        resp = await client.get(
            "https://api.hunter.io/v2/email-finder",
            params={
                "domain": domain,
                "first_name": first,
                "last_name": last,
                "api_key": HUNTER_API_KEY,
            },
        )
        data = resp.json().get("data", {})
        if data.get("email"):
            return data["email"], f"hunter:{data.get('score', '?')}"
    except Exception:
        log.debug("hunter lookup failed for %s.%s@%s", first, last, domain)
    return "", ""


def smtp_verify(candidates: list[str]) -> str:
    """Return the first candidate an MX server accepts (RCPT 250). Best-effort:
    many corporate servers block probes or accept everything (catch-all)."""
    if not candidates:
        return ""
    try:
        import smtplib

        import dns.resolver
    except ImportError:
        return ""
    domain = candidates[0].split("@", 1)[1]
    try:
        mx = sorted(
            dns.resolver.resolve(domain, "MX"),
            key=lambda r: r.preference,
        )[0].exchange.to_text()
    except Exception:
        return ""
    try:
        server = smtplib.SMTP(mx, 25, timeout=8)
        server.helo("mf-engine.local")
        server.mail("verify@mf-engine.local")
        # catch-all guard: if a random address is accepted, RCPT proves nothing
        rc, _ = server.rcpt(f"zzq-nonexistent-9182@{domain}")
        catch_all = rc in (250, 251)
        hit = ""
        if not catch_all:
            for cand in candidates:
                code, _ = server.rcpt(cand)
                if code in (250, 251):
                    hit = cand
                    break
        server.quit()
        return hit
    except Exception:
        return ""


async def main() -> int:
    if not INPUT_CSV.exists():
        log.error("Input missing — run phase3_extract.py first (%s)", INPUT_CSV)
        return 1
    rows = list(csv.DictReader(INPUT_CSV.open(encoding="utf-8")))
    if MAX_MANAGERS:
        rows = rows[:MAX_MANAGERS]

    overrides = {}
    if OVERRIDES_PATH.exists():
        overrides = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    backend = "brave-api" if BRAVE_API_KEY else f"searxng({SEARXNG_URL})"
    log.info(
        "Enriching %d managers | search=%s overrides=%d hunter=%s smtp=%s",
        len(rows),
        backend,
        len(overrides),
        bool(HUNTER_API_KEY),
        VERIFY_SMTP,
    )

    async with httpx.AsyncClient(
        timeout=20.0, headers={"User-Agent": USER_AGENT}
    ) as client:

        async def enrich(row: dict) -> None:
            name = row["manager_name"]
            firm = row["firm_name"]
            first, last = name_parts(name)
            domain = domain_of(row.get("source_url", ""))

            override = overrides.get(f"{name}|{firm}")
            linkedin = override or await find_linkedin(client, name, firm)

            guesses = email_candidates(first, last, domain)
            row["email_guess"] = guesses[0] if guesses else ""

            verified = row.get("email", "")  # kept from the AMC page
            source = "amc_page" if verified else ""
            if not verified:
                verified, source = await hunter_email(client, first, last, domain)
            if not verified and VERIFY_SMTP:
                hit = await asyncio.to_thread(smtp_verify, guesses)
                if hit:
                    verified, source = hit, "smtp"

            row["email"] = verified
            row["email_source"] = source
            row["linkedin_url"] = linkedin
            log.info(
                "%-26s %-22s li=%-3s email=%s",
                firm[:26],
                name[:22],
                "yes" if linkedin else "no",
                verified or ("guess:" + row["email_guess"] if row["email_guess"] else "-"),
            )

        # SearXNG aggregates engines but still scrapes them under the hood, so
        # a burst gets the upstream engines (Bing/Google) IP-throttled and they
        # start returning junk (the name-match guard rejects it, but coverage
        # drops). Run serially with a gap between live searches to stay under
        # engine limits; override hits skip search and skip the wait.
        for i, row in enumerate(rows):
            had_override = f"{row['manager_name']}|{row['firm_name']}" in overrides
            await enrich(row)
            if not had_override and i + 1 < len(rows):
                await asyncio.sleep(SEARCH_GAP_SECONDS)

    fields = [
        "firm_name", "manager_name", "designation", "location",
        "email", "email_source", "email_guess", "linkedin_url", "source_url",
    ]
    with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    li = sum(1 for r in rows if r.get("linkedin_url"))
    em = sum(1 for r in rows if r.get("email"))
    log.info(
        "Wrote %s — %d/%d with LinkedIn, %d with a verified email",
        OUTPUT_CSV,
        li,
        len(rows),
        em,
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
