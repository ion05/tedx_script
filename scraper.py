#!/usr/bin/env python3
"""
Purdue Dean's List → Directory Email Scraper

Fetches student names from the Purdue College of Science honors page,
looks up each name in the Purdue Directory to find their alias,
and writes name + alias + email + status to a CSV file.

Usage:
    python scraper.py [--semester CODE [CODE ...]] [--max-names N] [--delay SECONDS]

Examples:
    python scraper.py                                    # Default semesters
    python scraper.py --semester 202520                  # Spring 2025 only
    python scraper.py --semester 202520 202310 202320    # Multiple semesters
    python scraper.py --max-names 50                     # First 50 unique names
    python scraper.py --delay 0.5                        # 0.5s between lookups
"""

import argparse
import csv
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HONORS_API_URL = (
    "https://www.science.purdue.edu/php-scripts/certificate/server_processing.php"
)
DIRECTORY_URL = "https://www.purdue.edu/directory/"

FETCH_ALL_LENGTH = 70000  # grab every row in one request; API supports it

SEMESTER_LABELS = {
    "202610": "Fall 2025",
    "202530": "Summer 2025",
    "202520": "Spring 2025",
    "202510": "Fall 2024",
    "202420": "Spring 2024",
    "202410": "Fall 2023",
    "202320": "Spring 2023",
    "202310": "Fall 2022",
    "202220": "Spring 2022",
    "202210": "Fall 2021",
    "202120": "Spring 2021",
    "202110": "Fall 2020",
    "202020": "Spring 2020",
    "202010": "Fall 2019",
    "201920": "Spring 2019",
    "201910": "Fall 2018",
}

DEFAULT_SEMESTERS = ["202520", "202310", "202320"]  # Spring 2025, Fall 2022, Spring 2023

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
OUTPUT_CSV = os.path.join(OUTPUT_DIR, "emails.csv")

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class StudentRecord:
    full_name: str
    first_name: str
    last_name: str
    semester: str
    award: str
    alias: Optional[str] = None
    email: Optional[str] = None
    status: str = "pending"

# ---------------------------------------------------------------------------
# Step 1 — Fetch names from the honors DataTables API
# ---------------------------------------------------------------------------

def fetch_honors_names(
    semesters: list[str],
    max_names: Optional[int] = None,
    session: Optional[requests.Session] = None,
) -> list[StudentRecord]:
    """
    Fetch all rows from the server-side DataTables endpoint in one request,
    then filter client-side by one or more semesters and deduplicate by
    first+last name.
    """
    sess = session or requests.Session()
    semester_labels = {SEMESTER_LABELS[s] for s in semesters if s in SEMESTER_LABELS}
    labels_display = ", ".join(sorted(semester_labels)) or "all"

    print(f"[honors] Fetching all records from honors API …")

    params = {"draw": 1, "start": 0, "length": FETCH_ALL_LENGTH}
    try:
        resp = sess.get(HONORS_API_URL, params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"[honors] Error fetching data: {exc}")
        return []

    all_rows = data.get("data", [])
    total = data.get("recordsTotal", "?")
    print(f"[honors] Received {len(all_rows)} rows (server total: {total}).")
    print(f"[honors] Filtering for semesters: {labels_display} …")

    seen_keys: set[str] = set()
    records: list[StudentRecord] = []

    for row in all_rows:
        row_semester = row.get("1", "")
        if semester_labels and row_semester not in semester_labels:
            continue

        full_name = row.get("0", "").strip()
        first_name = row.get("4", "").strip()
        last_name = row.get("5", "").strip()
        award = row.get("2", "").strip()

        if not full_name or not first_name or not last_name:
            continue

        dedup_key = f"{first_name.lower()}|{last_name.lower()}"
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        records.append(
            StudentRecord(
                full_name=full_name,
                first_name=first_name,
                last_name=last_name,
                semester=row_semester,
                award=award,
            )
        )

        if max_names and len(records) >= max_names:
            break

    print(f"[honors] Collected {len(records)} unique student names.")
    return records

# ---------------------------------------------------------------------------
# Step 2 — Look up alias in the Purdue Directory (requests + BS4)
# ---------------------------------------------------------------------------

def _normalize(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    name = name.lower().strip()
    name = re.sub(r"[^a-z\s]", "", name)
    return re.sub(r"\s+", " ", name).strip()


def _name_matches(directory_name: str, first: str, last: str) -> bool:
    """
    Check whether a directory entry plausibly matches the student.
    The directory returns full names like "john l smith jr".
    We require last-name match and first-name match (prefix or full).
    """
    dn = _normalize(directory_name)
    fn = _normalize(first)
    ln = _normalize(last)

    parts = dn.split()
    if not parts:
        return False

    last_matches = ln in parts or parts[-1] == ln
    first_matches = parts[0] == fn or parts[0].startswith(fn[:3])

    return last_matches and first_matches


def lookup_directory_requests(
    record: StudentRecord,
    session: requests.Session,
) -> None:
    """
    POST to the Purdue Directory, parse the HTML response, and populate
    record.alias / record.email / record.status.
    """
    search_name = f"{record.first_name} {record.last_name}"
    try:
        resp = session.post(
            DIRECTORY_URL,
            data={"SearchString": search_name},
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as exc:
        record.status = "error_request"
        print(f"  [dir] HTTP error for '{search_name}': {exc}")
        return

    soup = BeautifulSoup(resp.text, "lxml")
    results_section = soup.find("section", id="results")
    if not results_section:
        record.status = "unmatched"
        return

    people = results_section.find_all("li")
    if not people:
        record.status = "unmatched"
        return

    matches: list[dict] = []
    for person_li in people:
        cn_tag = person_li.find("h2", class_="cn-name")
        if not cn_tag:
            continue
        cn_name = cn_tag.get_text(strip=True)

        alias_tag = person_li.find("th", class_="icon-key")
        alias = None
        if alias_tag:
            alias_td = alias_tag.find_next_sibling("td")
            if alias_td:
                alias = alias_td.get_text(strip=True)

        email_tag = person_li.find("th", class_="icon-envelope-alt")
        email = None
        if email_tag:
            email_td = email_tag.find_next_sibling("td")
            if email_td:
                a_tag = email_td.find("a")
                if a_tag:
                    email = a_tag.get_text(strip=True)

        if _name_matches(cn_name, record.first_name, record.last_name):
            matches.append({"alias": alias, "email": email, "cn": cn_name})

    if len(matches) >= 1:
        m = matches[0]
        record.alias = m["alias"]
        record.email = m["email"] or (f"{m['alias']}@purdue.edu" if m["alias"] else None)
        record.status = "matched"
    else:
        if people:
            first_person = people[0]
            alias_tag = first_person.find("th", class_="icon-key")
            if alias_tag:
                alias_td = alias_tag.find_next_sibling("td")
                if alias_td:
                    record.alias = alias_td.get_text(strip=True)
                    record.email = f"{record.alias}@purdue.edu"
                    record.status = "matched"
                    return
        record.status = "unmatched"

# ---------------------------------------------------------------------------
# Step 2b — Playwright fallback (only for error/unmatched from requests)
# ---------------------------------------------------------------------------

_BROWSER_LAUNCHED = False
_PW_CONTEXT = None


def _ensure_playwright():
    """Lazy-init a Playwright browser context."""
    global _BROWSER_LAUNCHED, _PW_CONTEXT
    if _BROWSER_LAUNCHED:
        return _PW_CONTEXT

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[browser] playwright not installed — skipping browser fallback.")
        return None

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    _PW_CONTEXT = browser.new_context()
    _BROWSER_LAUNCHED = True
    return _PW_CONTEXT


def lookup_directory_browser(record: StudentRecord) -> None:
    """Fallback: use Playwright to search the directory and parse results."""
    ctx = _ensure_playwright()
    if ctx is None:
        return

    page = ctx.new_page()
    try:
        page.goto(DIRECTORY_URL, timeout=15000)
        search_box = page.locator("#basicSearchInput")
        search_box.wait_for(state="visible", timeout=5000)

        search_name = f"{record.first_name} {record.last_name}"
        search_box.fill(search_name)
        page.keyboard.press("Enter")
        page.wait_for_load_state("networkidle", timeout=10000)

        html = page.content()
        soup = BeautifulSoup(html, "lxml")
        results_section = soup.find("section", id="results")
        if not results_section:
            record.status = "unmatched_browser"
            return

        people = results_section.find_all("li")
        for person_li in people:
            cn_tag = person_li.find("h2", class_="cn-name")
            if not cn_tag:
                continue
            cn_name = cn_tag.get_text(strip=True)

            if not _name_matches(cn_name, record.first_name, record.last_name):
                continue

            alias_tag = person_li.find("th", class_="icon-key")
            if alias_tag:
                alias_td = alias_tag.find_next_sibling("td")
                if alias_td:
                    record.alias = alias_td.get_text(strip=True)
                    record.email = f"{record.alias}@purdue.edu"
                    record.status = "matched"
                    return

        record.status = "unmatched"
    except Exception as exc:
        print(f"  [browser] Error for '{record.full_name}': {exc}")
        record.status = "error_request"
    finally:
        page.close()

# ---------------------------------------------------------------------------
# Step 3 — Write CSV
# ---------------------------------------------------------------------------

def write_csv(records: list[StudentRecord], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["name", "semester", "alias", "email", "status"])
        for r in records:
            writer.writerow([r.full_name, r.semester, r.alias or "", r.email or "", r.status])
    print(f"\n[csv] Wrote {len(records)} rows → {path}")

# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run(semesters: list[str], max_names: Optional[int], delay: float, use_browser_fallback: bool) -> None:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
    )

    # 1. Fetch names
    records = fetch_honors_names(semesters, max_names=max_names, session=session)
    if not records:
        print("[!] No names found. Check semester code or network.")
        sys.exit(1)

    # 2. Directory lookups
    total = len(records)
    print(f"\n[dir] Looking up {total} names in Purdue Directory …\n")
    for idx, rec in enumerate(records, 1):
        print(f"  [{idx}/{total}] {rec.full_name} … ", end="", flush=True)
        lookup_directory_requests(rec, session)
        print(f"{rec.status}" + (f"  →  {rec.email}" if rec.email else ""))

        if delay > 0:
            time.sleep(delay)

    # 2b. Browser fallback for failures
    need_fallback = [r for r in records if r.status in ("unmatched", "error_request")]
    if need_fallback and use_browser_fallback:
        print(f"\n[browser] Retrying {len(need_fallback)} names with Playwright …\n")
        for idx, rec in enumerate(need_fallback, 1):
            print(f"  [{idx}/{len(need_fallback)}] {rec.full_name} … ", end="", flush=True)
            lookup_directory_browser(rec)
            print(f"{rec.status}" + (f"  →  {rec.email}" if rec.email else ""))
            if delay > 0:
                time.sleep(delay)

    # 3. Write CSV
    write_csv(records, OUTPUT_CSV)

    # Summary
    statuses: dict[str, int] = {}
    for r in records:
        statuses[r.status] = statuses.get(r.status, 0) + 1
    print("\n[summary]")
    for s, cnt in sorted(statuses.items()):
        print(f"  {s}: {cnt}")

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape Purdue honors names → directory aliases → CSV emails."
    )
    parser.add_argument(
        "--semester",
        nargs="+",
        default=DEFAULT_SEMESTERS,
        help="One or more semester codes (default: %(default)s). "
        f"Codes: {', '.join(f'{k}={v}' for k, v in list(SEMESTER_LABELS.items())[:8])} …",
    )
    parser.add_argument(
        "--max-names",
        type=int,
        default=None,
        help="Cap the number of unique names to process (useful for testing).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.3,
        help="Seconds to wait between directory lookups (default: 0.3).",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Skip the Playwright browser fallback for unmatched names.",
    )
    args = parser.parse_args()

    run(
        semesters=args.semester,
        max_names=args.max_names,
        delay=args.delay,
        use_browser_fallback=not args.no_browser,
    )


if __name__ == "__main__":
    main()
