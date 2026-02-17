"""
College-specific name source providers.

Each provider fetches student names from a college's honors / Dean's List page
and returns them as StudentRecord instances grouped by semester label.
The directory lookup, email generation, CSV writing, and concurrency logic
live in scraper.py and are shared across all colleges.
"""

import re
from dataclasses import dataclass
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Shared data model
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

LABEL_TO_CODE = {v: k for k, v in SEMESTER_LABELS.items()}

# Registry: maps college name -> fetch function
_PROVIDERS: dict[str, object] = {}


def provider(college_name: str):
    """Decorator that registers a fetch function for *college_name*."""
    def _register(fn):
        _PROVIDERS[college_name] = fn
        return fn
    return _register


def get_provider(college: str):
    """Return the registered fetch function for *college*, or raise."""
    if college not in _PROVIDERS:
        supported = ", ".join(sorted(_PROVIDERS))
        raise ValueError(
            f"Unknown college '{college}'. Supported: {supported}"
        )
    return _PROVIDERS[college]


# ---------------------------------------------------------------------------
# Science provider
# ---------------------------------------------------------------------------

SCIENCE_API_URL = (
    "https://www.science.purdue.edu/php-scripts/certificate/server_processing.php"
)
FETCH_ALL_LENGTH = 70000


@provider("science")
def fetch_science_names(
    semesters: list[str],
    max_names: Optional[int] = None,
    session: Optional[requests.Session] = None,
) -> dict[str, list[StudentRecord]]:
    """
    Fetch all rows from the College of Science server-side DataTables
    endpoint in one request, filter by semester, deduplicate by first+last.
    """
    sess = session or requests.Session()
    semester_labels = {SEMESTER_LABELS[s] for s in semesters if s in SEMESTER_LABELS}
    labels_display = ", ".join(sorted(semester_labels)) or "all"

    print(f"[science] Fetching all records from honors API …")

    params = {"draw": 1, "start": 0, "length": FETCH_ALL_LENGTH}
    try:
        resp = sess.get(SCIENCE_API_URL, params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        print(f"[science] Error fetching data: {exc}")
        return {}

    all_rows = data.get("data", [])
    total = data.get("recordsTotal", "?")
    print(f"[science] Received {len(all_rows)} rows (server total: {total}).")
    print(f"[science] Filtering for semesters: {labels_display} …")

    per_semester: dict[str, list[StudentRecord]] = {label: [] for label in semester_labels}
    seen_per_semester: dict[str, set[str]] = {label: set() for label in semester_labels}

    for row in all_rows:
        row_semester = row.get("1", "")
        if row_semester not in semester_labels:
            continue

        full_name = row.get("0", "").strip()
        first_name = row.get("4", "").strip()
        last_name = row.get("5", "").strip()
        award = row.get("2", "").strip()

        if not full_name or not first_name or not last_name:
            continue

        dedup_key = f"{first_name.lower()}|{last_name.lower()}"
        if dedup_key in seen_per_semester[row_semester]:
            continue
        seen_per_semester[row_semester].add(dedup_key)

        per_semester[row_semester].append(
            StudentRecord(
                full_name=full_name,
                first_name=first_name,
                last_name=last_name,
                semester=row_semester,
                award=award,
            )
        )

        if max_names:
            all_full = all(len(v) >= max_names for v in per_semester.values())
            if all_full:
                break
            if len(per_semester[row_semester]) >= max_names:
                continue

    for label, recs in per_semester.items():
        if max_names:
            per_semester[label] = recs[:max_names]
        print(f"[science]   {label}: {len(per_semester[label])} unique names")

    return per_semester


# ---------------------------------------------------------------------------
# Engineering provider
# ---------------------------------------------------------------------------

ENGINEERING_BASE_URL = (
    "https://engineering.purdue.edu"
    "/Engr/Academics/Undergraduate/AcademicHonors"
)
ENGINEERING_SEMESTER_LIST_URL = f"{ENGINEERING_BASE_URL}/semester-list"


def _discover_engineering_semesters(
    session: requests.Session,
) -> dict[str, str]:
    """
    Scrape the semester-list page to discover available semester URLs.
    Returns dict mapping semester label (e.g. "Fall 2025") -> full URL.
    """
    print("[engineering] Fetching semester list …")
    try:
        resp = session.get(ENGINEERING_SEMESTER_LIST_URL, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        print(f"[engineering] Error fetching semester list: {exc}")
        return {}

    soup = BeautifulSoup(resp.text, "lxml")
    semester_urls: dict[str, str] = {}

    for a_tag in soup.find_all("a"):
        text = a_tag.get_text(strip=True)
        href = a_tag.get("href", "")
        if re.match(r"^(Fall|Spring|Summer)\s+\d{4}$", text):
            if href and not href.startswith("http"):
                href = f"https://engineering.purdue.edu{href}"
            semester_urls[text] = href

    print(f"[engineering] Found {len(semester_urls)} semesters on list page.")
    return semester_urls


def _parse_engineering_page(
    html: str,
    semester_label: str,
    max_names: Optional[int] = None,
) -> list[StudentRecord]:
    """
    Parse student names from a single Engineering honors semester page.
    Names are <a> tags whose href contains '/certificate?id='.
    """
    soup = BeautifulSoup(html, "lxml")
    records: list[StudentRecord] = []
    seen: set[str] = set()

    for a_tag in soup.find_all("a", href=re.compile(r"/certificate\?id=")):
        full_name = a_tag.get_text(strip=True)
        if not full_name:
            continue

        full_name = re.sub(r"\s+", " ", full_name).strip()

        dedup_key = full_name.lower()
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        parts = full_name.split()
        if len(parts) < 2:
            continue
        first_name = parts[0]
        last_name = parts[-1]

        records.append(
            StudentRecord(
                full_name=full_name,
                first_name=first_name,
                last_name=last_name,
                semester=semester_label,
                award="",
            )
        )

        if max_names and len(records) >= max_names:
            break

    return records


@provider("engineering")
def fetch_engineering_names(
    semesters: list[str],
    max_names: Optional[int] = None,
    session: Optional[requests.Session] = None,
) -> dict[str, list[StudentRecord]]:
    """
    Scrape Engineering honors pages for the requested semesters.
    Returns dict mapping semester label -> list[StudentRecord].
    """
    sess = session or requests.Session()

    requested_labels = {SEMESTER_LABELS[s] for s in semesters if s in SEMESTER_LABELS}
    if not requested_labels:
        print("[engineering] No valid semester codes provided.")
        return {}

    semester_urls = _discover_engineering_semesters(sess)
    if not semester_urls:
        print("[engineering] Could not discover any semester URLs.")
        return {}

    per_semester: dict[str, list[StudentRecord]] = {}

    for label in sorted(requested_labels):
        if label not in semester_urls:
            print(f"[engineering] {label}: not found on semester list, skipping.")
            continue

        url = semester_urls[label]
        print(f"[engineering] Fetching {label} from {url} …")

        try:
            resp = sess.get(url, timeout=60)
            resp.raise_for_status()
        except Exception as exc:
            print(f"[engineering] Error fetching {label}: {exc}")
            continue

        records = _parse_engineering_page(resp.text, label, max_names=max_names)
        per_semester[label] = records
        print(f"[engineering]   {label}: {len(records)} unique names")

    return per_semester
