"""MF-Engine — SEBI registered mutual funds scraper (standalone).

Scrapes SEBI's official "Registered Mutual Funds" directory — the *regulator's*
list, complementary to the AMFI members roster the main pipeline seeds from.
SEBI carries each fund's registration number, registered address, and
registration date, which AMFI does not.

The public page (OtherAction.do?doRecognisedFpi=yes&intmId=23) paginates via an
AJAX call its own `searchFormFpi()` makes to `getintmfpiinfo.jsp`, POSTing
`doDirect=<page-1>` (0-based) and getting back an HTML fragment of ~25 records.
No token or browser is needed — we call that endpoint directly, page by page.

Output: data/sebi_mutual_funds.json

Usage:
    python src/sebi_mutual_funds.py
"""

import asyncio
import json
import logging
import re
import sys
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mf-engine.sebi")
logging.getLogger("httpx").setLevel(logging.WARNING)

AJAX_URL = "https://www.sebi.gov.in/sebiweb/ajax/other/getintmfpiinfo.jsp"
REFERER = (
    "https://www.sebi.gov.in/sebiweb/other/OtherAction.do"
    "?doRecognisedFpi=yes&intmId=23"
)
OUTPUT_PATH = Path("data/sebi_mutual_funds.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
INTM_ID = "23"  # SEBI intermediary type: Mutual Funds
MAX_PAGES = 12  # safety bound; the list is ~3 pages of 25
RETRIES = 4  # SEBI drops connections under rapid requests


def page_form(page_index: int) -> dict:
    """POST body searchFormFpi() sends; doDirect is the 0-based page index."""
    return {
        "nextValue": "1",
        "next": "n",
        "intmId": INTM_ID,
        "contPer": "",
        "name": "",
        "regNo": "",
        "email": "",
        "location": "",
        "exchange": "",
        "affiliate": "",
        "alp": "",
        "doDirect": str(page_index),
        "intmIds": "",
    }


def parse_records(html: str) -> list[dict]:
    """Group SEBI card-view label/value blocks into one record per fund."""
    soup = BeautifulSoup(html, "html.parser")
    container = soup.find(id="ajax_cat") or soup
    records: list[dict] = []
    current: dict | None = None
    for card in container.select("div.card-view"):
        parts = [p for p in card.get_text("||", strip=True).split("||") if p]
        if len(parts) < 2:
            continue
        label = parts[0].lower()
        value = " ".join(parts[1:]).strip()
        if label.startswith("name"):
            current = {"name": value, "reg_no": "", "address": "", "validity": ""}
            records.append(current)
        elif current is not None:
            if "regist" in label:
                current["reg_no"] = value
            elif "address" in label:
                current["address"] = value
            elif "valid" in label:
                current["validity"] = value
    return [r for r in records if r["reg_no"]]


def city_state(address: str) -> tuple[str, str]:
    """Best-effort (city, state) from a SEBI address tail: '..., CITY, STATE, PIN'."""
    tokens = [t.strip() for t in address.split(",") if t.strip()]
    tokens = [t for t in tokens if not re.fullmatch(r"\d{6}", t)]  # drop PIN
    if len(tokens) >= 2:
        return tokens[-2].title(), tokens[-1].title()
    return "", ""


async def fetch_page(client: httpx.AsyncClient, page_index: int) -> str:
    """POST the AJAX endpoint for one page, retrying flaky disconnects."""
    for attempt in range(RETRIES):
        try:
            resp = await client.post(AJAX_URL, data=page_form(page_index))
            if resp.status_code == 200 and resp.text:
                return resp.text
        except httpx.HTTPError:
            pass
        await asyncio.sleep(1.5 * (attempt + 1))
    log.warning("Page index %d failed after %d retries", page_index, RETRIES)
    return ""


async def main() -> int:
    log.info("Scraping SEBI registered mutual funds")
    seen: set[str] = set()
    records: list[dict] = []

    async with httpx.AsyncClient(
        timeout=25.0,
        headers={
            "User-Agent": USER_AGENT,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": REFERER,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    ) as client:
        for page_index in range(MAX_PAGES):
            html = await fetch_page(client, page_index)
            page_records = parse_records(html)
            new = [r for r in page_records if r["reg_no"] not in seen]
            for r in new:
                seen.add(r["reg_no"])
                r["city"], r["state"] = city_state(r["address"])
                records.append(r)
            log.info(
                "Page %d: %d records (%d new)", page_index + 1, len(page_records), len(new)
            )
            if not new:  # pager wrapped or ran out — done
                break
            await asyncio.sleep(1.0)  # be gentle; SEBI drops rapid connections

    if not records:
        log.error("No records parsed — SEBI endpoint or structure may have changed")
        return 1

    records.sort(key=lambda r: r["name"].lower())
    for i, r in enumerate(records, start=1):
        r["sebi_id"] = i
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log.info("Wrote %d SEBI-registered mutual funds to %s", len(records), OUTPUT_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
