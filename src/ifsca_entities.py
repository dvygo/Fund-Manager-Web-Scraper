"""MF-Engine — IFSCA (GIFT City) registered entities scraper.

IFSCA is the regulator for India's International Financial Services Centre at
GIFT City — a separate regime from SEBI's onshore one. Its directory lists the
funds and fund management entities domiciled there, which the SEBI directories
do not cover:

    fund-management       ~636  FMEs, Retail Schemes, Category I/II/III AIFs
    capital-market        ~      brokers, depository participants
    banking                      IFSC Banking Units
    finance-company
    insurance / iiio             IIO and IIIO offices
    ...                          see TYPES below

The public page renders nothing server-side. Its DataTable calls
/DirectoryList/DirectoryGetList (server-side paginated JSON), but that listing
returns *only* name, category and an EncryptedId — every contact field comes
back null. The detail the UI shows in its popup comes from a second endpoint,
/DirectoryList/DirectoryDetailGet?EncryptedId=..., one call per entity. So the
scrape is list-then-detail. No token or browser needed.

Output:
    data/ifsca_<type>.json
    data/csv/ifsca_<type>.csv
    data/csv/ifsca_entities.csv   (all scraped types rolled up)

Usage:
    python src/ifsca_entities.py                    # fund-management (default)
    python src/ifsca_entities.py banking insurance  # specific types
    python src/ifsca_entities.py --all              # every category
"""

import asyncio
import csv
import json
import logging
import re
import sys
from pathlib import Path

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mf-engine.ifsca")
logging.getLogger("httpx").setLevel(logging.WARNING)

DIRECTORY_URL = "https://ifsca.gov.in/DirectoryList"
API_URL = "https://ifsca.gov.in/DirectoryList/DirectoryGetList"
DETAIL_URL = "https://ifsca.gov.in/DirectoryList/DirectoryDetailGet"
DATA_DIR = Path("data")
CSV_DIR = DATA_DIR / "csv"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# IFSCA category -> the ParentTypeId its directory dropdown uses.
TYPES: dict[str, int] = {
    "fund-management": 7,
    "banking": 1,
    "capital-market": 3,
    "finance-company": 2,
    "insurance-iio": 4,
    "insurance-iiio": 5,
    "ancillary-service": 9,
    "global-in-house-centres": 10,
    "metals-commodities": 11,
    "kyc-registration-agency": 19,
    "qualified-jewellers": 21,
    "payment-system-provider": 22,
    "market-infrastructure": 23,
    "fintech-sandbox": 24,
    "payment-service-provider": 26,
    "batf-service-providers": 27,
    "tas-service-provider": 28,
    "foreign-universities": 29,
    "surrendered-cancelled": 30,
}
DEFAULT_TYPES = ["fund-management"]

PAGE_SIZE = 100
RETRIES = 4
PAGE_DELAY = 0.6
MAX_PAGES = 200
DETAIL_CONCURRENCY = 5  # one call per entity; keep it civil

CSV_COLUMNS = [
    "ifsca_id", "name", "category", "sub_category", "reg_no", "date_of_registration",
    "validity_from", "validity_to", "contact_person", "email", "telephone",
    "website", "domain", "pincode", "address", "remarks", "ifsca_type",
]


def clean(value) -> str:
    """Trim, drop HTML entities/tags the CMS leaves in free-text fields."""
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(value))
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text).strip()


EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")


def fix_contact(email: str, telephone: str) -> tuple[str, str]:
    """Untangle IFSCA's swapped contact columns.

    ~9% of rows carry a phone number in Email and the address in Telephone
    (e.g. '+91 9892840799' as the e-mail). Trust the shape of the value, not
    the column it arrived in, and drop whatever is neither.
    """
    blob = f"{email} {telephone}"
    found = EMAIL_RE.search(blob)
    # 'nikhil rathi@360.one' — a space snuck into the local part
    if not found:
        found = EMAIL_RE.search(blob.replace(" ", ""))
    good_email = found.group(0).lower() if found else ""

    phone = ""
    for candidate in (telephone, email):
        if candidate and "@" not in candidate:
            digits = re.sub(r"\D", "", candidate)
            if 7 <= len(digits) <= 15:
                phone = candidate.strip()
                break
    return good_email, phone


def website_domain(website: str) -> str:
    site = clean(website).lower()
    if not site or "@" in site:
        return ""
    site = re.sub(r"^https?://", "", site)
    site = site.split("/")[0].split()[0] if site else ""
    return site.removeprefix("www.")


def extract_pincode(address: str) -> str:
    """6-digit PIN — the join key into data/pincodes.json. Indian PINs never
    start with 0, which rules out phone fragments."""
    hits = re.findall(r"\b[1-9]\d{5}\b", address or "")
    return hits[-1] if hits else ""


def parse_detail(row: dict, detail: dict | None) -> dict:
    """Merge a listing row with its detail payload.

    The listing only carries Title/category/EncryptedId; everything a caller
    actually wants (reg no, address, e-mail, website) is detail-only.
    """
    d = detail or {}
    address = clean(d.get("RegisteredAddress"))
    website = clean(d.get("Website"))
    email, telephone = fix_contact(
        clean(d.get("Email") or d.get("EmailId")),
        clean(d.get("ContactNumber")),
    )
    return {
        "name": clean(d.get("Title") or row.get("Title")),
        "category": clean(row.get("DirParentCategoryName")),
        # the detail's DfType is the precise one (e.g. "Retail Scheme")
        "sub_category": clean(d.get("DfType") or row.get("DirSubCategoryName")),
        "reg_no": clean(d.get("RegistrationNumber")),
        "date_of_registration": clean(d.get("DateOfRegistration")),
        "validity_from": clean(d.get("ValidityFromDate")),
        "validity_to": clean(d.get("ValidityToDate")),
        "contact_person": clean(d.get("NameofContactPerson")),
        "email": email,
        "telephone": telephone,
        "website": website,
        "domain": website_domain(website),
        "pincode": extract_pincode(address),
        "address": address,
        "remarks": clean(d.get("Remarks")),
    }


async def fetch_detail(client: httpx.AsyncClient, encrypted_id: str) -> dict | None:
    """One entity's detail popup payload."""
    if not encrypted_id:
        return None
    for attempt in range(RETRIES):
        try:
            resp = await client.get(DETAIL_URL, params={"EncryptedId": encrypted_id})
            if resp.status_code == 200:
                return resp.json()
        except (httpx.HTTPError, ValueError):
            pass
        await asyncio.sleep(1.0 * (attempt + 1))
    return None


async def fetch_page(
    client: httpx.AsyncClient, parent_type_id: int, page: int
) -> dict | None:
    params = {
        "draw": 1,
        "start": (page - 1) * PAGE_SIZE,
        "length": PAGE_SIZE,
        "PageNumber": page,
        "PageSize": PAGE_SIZE,
        "SearchText": "",
        "SearchName": "",
        "Id": 0,
        "ParentTypeId": parent_type_id,
        "EntityFilter": "",
    }
    for attempt in range(RETRIES):
        try:
            resp = await client.get(API_URL, params=params)
            if resp.status_code == 200:
                return resp.json()
        except (httpx.HTTPError, ValueError):
            pass
        await asyncio.sleep(1.5 * (attempt + 1))
    log.warning("ParentTypeId=%d page %d failed after %d tries", parent_type_id, page, RETRIES)
    return None


async def scrape_type(client: httpx.AsyncClient, slug: str) -> list[dict]:
    parent_type_id = TYPES[slug]
    listing: list[dict] = []
    seen: set[str] = set()
    expected = 0

    # 1. walk the paginated listing for names + EncryptedIds
    for page in range(1, MAX_PAGES + 1):
        payload = await fetch_page(client, parent_type_id, page)
        if not payload:
            break
        rows = payload.get("data") or []
        if not rows:
            break
        if page == 1:
            expected = rows[0].get("PaginationRequest", {}).get("TotalRecord", 0)
            log.info("%s (ParentTypeId=%d): %d listed", slug, parent_type_id, expected)

        new = 0
        for raw in rows:
            eid = clean(raw.get("EncryptedId"))
            key = eid or f"{clean(raw.get('Title'))}".lower()
            if not key or key in seen:
                continue
            seen.add(key)
            listing.append(raw)
            new += 1
        if not new or (expected and len(listing) >= expected):
            break
        await asyncio.sleep(PAGE_DELAY)

    # 2. one detail call per entity — that's where the contact data lives
    log.info("  %s: fetching detail for %d entities...", slug, len(listing))
    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

    async def resolve(raw: dict) -> dict:
        async with sem:
            detail = await fetch_detail(client, clean(raw.get("EncryptedId")))
        rec = parse_detail(raw, detail)
        rec["ifsca_type"] = slug
        return rec

    records = await asyncio.gather(*(resolve(r) for r in listing))
    records = [r for r in records if r["name"]]
    records.sort(key=lambda r: r["name"].lower())
    for i, r in enumerate(records, start=1):
        r["ifsca_id"] = i
    return records


def write_csv(path: Path, records: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


async def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    slugs = list(TYPES) if "--all" in sys.argv else (args or DEFAULT_TYPES)

    unknown = [s for s in slugs if s not in TYPES]
    if unknown:
        log.error("Unknown type(s): %s — known: %s", ", ".join(unknown), ", ".join(TYPES))
        return 1

    log.info("Scraping IFSCA directory: %s", ", ".join(slugs))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CSV_DIR.mkdir(parents=True, exist_ok=True)
    combined: list[dict] = []

    async with httpx.AsyncClient(
        timeout=60.0,
        follow_redirects=True,
        headers={
            "User-Agent": USER_AGENT,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": DIRECTORY_URL,
        },
    ) as client:
        await client.get(DIRECTORY_URL)  # pick up session cookies
        for slug in slugs:
            records = await scrape_type(client, slug)
            if not records:
                log.error("%s: nothing parsed — endpoint or fields may have changed", slug)
                continue
            stem = f"ifsca_{slug.replace('-', '_')}"
            (DATA_DIR / f"{stem}.json").write_text(
                json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            write_csv(CSV_DIR / f"{stem}.csv", records)
            combined.extend(records)
            log.info(
                "Wrote %d %s — %d with website, %d with e-mail",
                len(records),
                slug,
                sum(1 for r in records if r["domain"]),
                sum(1 for r in records if r["email"]),
            )

    if combined:
        write_csv(CSV_DIR / "ifsca_entities.csv", combined)
        log.info("Wrote %d rows to %s", len(combined), CSV_DIR / "ifsca_entities.csv")
    log.info("Done — %d records across %d type(s)", len(combined), len(slugs))
    return 0 if combined else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
