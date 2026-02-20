#!/usr/bin/env python3
"""
Purdue Dean's List → Directory Email Scraper

Fetches student names from a college's honors page (Science, Engineering, …),
looks up each name in the Purdue Directory to find their alias,
and writes name + alias + email + status to a CSV file per semester.
CSV is saved incrementally every 50 lookups so progress is never lost.

Usage:
    python scraper.py [--semester CODE [CODE ...]] [--college NAME] [--max-names N]
                      [--delay SECONDS] [--workers N]

Examples:
    python scraper.py                                    # Default (science)
    python scraper.py --college engineering --semester 202610 202510 202410 202310
    python scraper.py --semester 202520                  # Spring 2025 only
    python scraper.py --college engineering              # Engineering, default semesters
    python scraper.py --workers 10 --delay 0.05          # Fast parallel lookups
    python scraper.py --max-names 50                     # First 50 unique names
"""

import argparse
import csv
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import Optional

import requests
from bs4 import BeautifulSoup

from name_sources import (
    StudentRecord,
    SEMESTER_LABELS,
    get_provider,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DIRECTORY_URL = "https://www.purdue.edu/directory/"

DEFAULT_SEMESTERS = ["202520", "202310", "202320"]
DEFAULT_COLLEGE = "science"
PRINT_EVERY = 50
SAVE_EVERY = 50

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

# ---------------------------------------------------------------------------
# Output directory helpers
# ---------------------------------------------------------------------------

def _output_dir_for_college(college: str) -> str:
    """
    Science keeps the original output/ path (backward compatible).
    Every other college gets output/<college>/.
    """
    if college == "science":
        return OUTPUT_DIR
    return os.path.join(OUTPUT_DIR, college)

# ---------------------------------------------------------------------------
# Step 2 — Look up alias in the Purdue Directory (shared for all colleges)
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


def lookup_directory(
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
# CSV helpers
# ---------------------------------------------------------------------------

def _semester_label_to_filename(college: str, semester_label: str) -> str:
    """
    Convert e.g. college='science', semester_label='Fall 2025'
    to 'science_emails_fall_2025.csv'.
    """
    slug = semester_label.lower().replace(" ", "_")
    return f"{college}_emails_{slug}.csv"


def write_csv(records: list[StudentRecord], path: str) -> None:
    """Write all records to CSV (overwrites existing file)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["name", "semester", "alias", "email", "status"])
        for r in records:
            writer.writerow([r.full_name, r.semester, r.alias or "", r.email or "", r.status])


def write_unique_combined_csv(
    all_records: list[StudentRecord],
    college: str,
) -> str:
    """
    Collect all matched email addresses across every semester, deduplicate,
    write to <college>_all_unique.csv, and return the output path.
    """
    seen_emails: set[str] = set()
    unique_rows: list[tuple[str, str]] = []

    for r in all_records:
        if r.email and r.email not in seen_emails:
            seen_emails.add(r.email)
            unique_rows.append((r.email, r.alias or ""))

    out_dir = _output_dir_for_college(college)
    out_path = os.path.join(out_dir, f"{college}_all_unique_emails.csv")
    os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["email", "alias"])
        for email, alias in sorted(unique_rows):
            writer.writerow([email, alias])

    return out_path

# ---------------------------------------------------------------------------
# Concurrent directory lookup with incremental CSV saves
# ---------------------------------------------------------------------------

_counter_lock = Lock()


def _lookup_worker(
    record: StudentRecord,
    session: requests.Session,
    delay: float,
) -> StudentRecord:
    """Worker function for threaded directory lookups."""
    lookup_directory(record, session)
    if delay > 0:
        time.sleep(delay)
    return record


def lookup_batch_concurrent(
    records: list[StudentRecord],
    session: requests.Session,
    delay: float,
    workers: int,
    csv_path: str,
) -> None:
    """Look up a list of records using a thread pool, saving CSV every SAVE_EVERY."""
    total = len(records)
    done = 0
    matched = 0
    t0 = time.time()

    print(f"[dir] Looking up {total} names ({workers} workers, {delay}s delay) …")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_lookup_worker, rec, session, delay): rec
            for rec in records
        }
        for future in as_completed(futures):
            rec = future.result()
            with _counter_lock:
                done += 1
                if rec.status == "matched":
                    matched += 1

                should_save = (done % SAVE_EVERY == 0) or (done == total)
                should_print = (done % PRINT_EVERY == 0) or (done == total)

            if should_save:
                write_csv(records, csv_path)

            if should_print:
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                print(
                    f"  [{done}/{total}]  "
                    f"matched={matched}  "
                    f"({rate:.1f}/sec, {elapsed:.0f}s)  "
                    f"[saved]" if should_save else
                    f"  [{done}/{total}]  "
                    f"matched={matched}  "
                    f"({rate:.1f}/sec, {elapsed:.0f}s)"
                )
                sys.stdout.flush()

# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run(
    semesters: list[str],
    college: str,
    max_names: Optional[int],
    delay: float,
    workers: int,
) -> None:
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

    # Resolve the provider for the requested college
    fetch_fn = get_provider(college)

    out_dir = _output_dir_for_college(college)

    # 1. Fetch names via the college-specific provider
    per_semester = fetch_fn(semesters, max_names=max_names, session=session)
    if not per_semester or all(len(v) == 0 for v in per_semester.values()):
        print("[!] No names found. Check semester codes or network.")
        sys.exit(1)

    # 2. Process each semester -> separate CSV
    all_records: list[StudentRecord] = []

    for semester_label, records in per_semester.items():
        if not records:
            print(f"\n[skip] {semester_label}: no records, skipping.")
            continue

        filename = _semester_label_to_filename(college, semester_label)
        csv_path = os.path.join(out_dir, filename)

        print(f"\n{'='*60}")
        print(f"  {semester_label}  ({len(records)} names)")
        print(f"  -> {csv_path}")
        print(f"{'='*60}\n")

        lookup_batch_concurrent(records, session, delay, workers, csv_path)

        write_csv(records, csv_path)

        statuses: dict[str, int] = {}
        for r in records:
            statuses[r.status] = statuses.get(r.status, 0) + 1
        print(f"\n[summary] {semester_label}:")
        for s, cnt in sorted(statuses.items()):
            print(f"  {s}: {cnt}")
        print(f"[csv] {csv_path}")

        all_records.extend(records)

    # 3. Combined unique-email CSV across all semesters
    if len(per_semester) > 1:
        unique_path = write_unique_combined_csv(all_records, college)
        with open(unique_path, newline="", encoding="utf-8") as fh:
            unique_count = sum(1 for _ in fh) - 1
        print(f"\n{'='*60}")
        print(f"  Combined unique emails: {unique_count}")
        print(f"  -> {unique_path}")
        print(f"{'='*60}")

    print(f"\n[done] All semesters processed. Files in {out_dir}/")

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape Purdue honors names -> directory aliases -> CSV emails."
    )
    parser.add_argument(
        "--semester",
        nargs="+",
        default=DEFAULT_SEMESTERS,
        help="One or more semester codes (default: %(default)s). "
        f"Codes: {', '.join(f'{k}={v}' for k, v in list(SEMESTER_LABELS.items())[:8])} …",
    )
    parser.add_argument(
        "--college",
        default=DEFAULT_COLLEGE,
        choices=["science", "engineering", "polytechnic", "cla", "agriculture"],
        help="Which college to scrape (default: %(default)s).",
    )
    parser.add_argument(
        "--max-names",
        type=int,
        default=None,
        help="Cap the number of unique names per semester (useful for testing).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.1,
        help="Seconds to wait between directory lookups per worker (default: 0.1).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Number of concurrent lookup threads (default: 8).",
    )
    args = parser.parse_args()

    run(
        semesters=args.semester,
        college=args.college,
        max_names=args.max_names,
        delay=args.delay,
        workers=args.workers,
    )


if __name__ == "__main__":
    main()
