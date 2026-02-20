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


# ---------------------------------------------------------------------------
# Polytechnic provider
# ---------------------------------------------------------------------------

# Only two Polytechnic Dean's List pages exist.
POLYTECHNIC_SEMESTER_URLS: dict[str, str] = {
    "Fall 2025": "https://polytechnic.purdue.edu/office-dean/deans-list",
    "Spring 2024": (
        "https://old.polytechnic.purdue.edu"
        "/undergraduate-studies/deans-list/spring-2024-deans-list"
    ),
}


def _parse_polytechnic_page(
    html: str,
    semester_label: str,
    max_names: Optional[int] = None,
) -> list[StudentRecord]:
    """
    Parse student names from a Polytechnic Dean's List page.

    Both the new site and the old site render names as the first <td> of each
    table row in "Last, First [M.]" format (e.g. "Smith, Jane A.").
    Award text lives in the second column on the old site and the third column
    on the new site; we read whichever is available.
    """
    soup = BeautifulSoup(html, "lxml")
    records: list[StudentRecord] = []
    seen: set[str] = set()

    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if not cells:
            continue

        name_text = cells[0].get_text(strip=True)

        # Must contain a comma and not be a header cell
        if not name_text or "," not in name_text or name_text.lower().startswith("name"):
            continue

        parts = name_text.split(",", 1)
        if len(parts) < 2:
            continue

        last_name = parts[0].strip().title()
        first_rest = parts[1].strip()
        first_tokens = first_rest.split()
        if not first_tokens:
            continue
        first_name = first_tokens[0].title()

        if not first_name or not last_name:
            continue

        dedup_key = f"{first_name.lower()}|{last_name.lower()}"
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        # Grab award from whichever column is available (column 1 on old site,
        # column 2 on new site); fall back to a generic label.
        if len(cells) >= 3:
            award = cells[2].get_text(strip=True)
        elif len(cells) >= 2:
            award = cells[1].get_text(strip=True)
        else:
            award = "Dean's List"

        records.append(
            StudentRecord(
                full_name=f"{first_name} {last_name}",
                first_name=first_name,
                last_name=last_name,
                semester=semester_label,
                award=award,
            )
        )

        if max_names and len(records) >= max_names:
            break

    return records


@provider("polytechnic")
def fetch_polytechnic_names(
    semesters: list[str],
    max_names: Optional[int] = None,
    session: Optional[requests.Session] = None,
) -> dict[str, list[StudentRecord]]:
    """
    Scrape Polytechnic Dean's List pages for the requested semesters.

    Fall 2025 and later are fetched from the current site; earlier semesters
    are fetched from the archived old site.
    Returns dict mapping semester label -> list[StudentRecord].
    """
    sess = session or requests.Session()

    requested_labels = {SEMESTER_LABELS[s] for s in semesters if s in SEMESTER_LABELS}
    if not requested_labels:
        print("[polytechnic] No valid semester codes provided.")
        return {}

    available = set(POLYTECHNIC_SEMESTER_URLS)
    per_semester: dict[str, list[StudentRecord]] = {}

    for label in sorted(requested_labels):
        if label not in available:
            print(
                f"[polytechnic] {label}: no data available. "
                f"Supported: {', '.join(sorted(available))}"
            )
            continue

        url = POLYTECHNIC_SEMESTER_URLS[label]
        print(f"[polytechnic] Fetching {label} from {url} …")

        try:
            resp = sess.get(url, timeout=60)
            resp.raise_for_status()
        except Exception as exc:
            print(f"[polytechnic] Error fetching {label}: {exc}")
            continue

        records = _parse_polytechnic_page(resp.text, label, max_names=max_names)
        if not records:
            print(f"[polytechnic]   {label}: 0 names found (page may not exist or format changed).")
        else:
            per_semester[label] = records
            print(f"[polytechnic]   {label}: {len(records)} unique names")

    return per_semester


# ---------------------------------------------------------------------------
# CLA (College of Liberal Arts) provider
# ---------------------------------------------------------------------------

CLA_BASE_URL = "https://www.cla.purdue.edu/students/awards-honors/deans-list"

CLA_SEMESTER_URLS: dict[str, str] = {
    "Fall 2025": f"{CLA_BASE_URL}/index.html",
    "Spring 2025": f"{CLA_BASE_URL}/deans-list-s25.html",
    "Fall 2024": f"{CLA_BASE_URL}/deans-list-f24.html",
    "Spring 2024": f"{CLA_BASE_URL}/deans-list-s24.html",
    "Fall 2023": f"{CLA_BASE_URL}/deans-list-f23.html",
    "Fall 2022": f"{CLA_BASE_URL}/deans-list-f22.html",
}

_CLA_SKIP_PREFIXES = ("key:", "dean's", "#", "a |", "b |")


def _parse_cla_page(
    html: str,
    semester_label: str,
    max_names: Optional[int] = None,
) -> list[StudentRecord]:
    """
    Parse student names from a CLA Dean's List page.

    Names appear as plain text lines in "Last, First [M.] [* [+]]" format.
    ``*`` indicates Dean's List; ``+`` indicates Semester Honors.
    """
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(separator="\n")

    records: list[StudentRecord] = []
    seen: set[str] = set()

    for line in text.splitlines():
        line = line.strip()
        if not line or "," not in line:
            continue

        low = line.lower()
        if any(low.startswith(p) for p in _CLA_SKIP_PREFIXES):
            continue
        if "back to top" in low:
            continue

        parts = line.split(",", 1)
        if len(parts) < 2:
            continue

        last_name = parts[0].strip()
        rest = parts[1].strip()

        if not last_name or not last_name[0].isalpha():
            continue
        if not rest or not rest[0].isalpha():
            continue

        has_star = "*" in rest
        has_plus = "+" in rest
        if has_star and has_plus:
            award = "Dean's List + Semester Honors"
        elif has_star:
            award = "Dean's List"
        elif has_plus:
            award = "Semester Honors"
        else:
            award = ""

        rest_clean = rest.replace("*", "").replace("+", "").strip()
        rest_clean = re.sub(r"\s+", " ", rest_clean).strip()

        tokens = rest_clean.split()
        if not tokens:
            continue

        first_name = tokens[0]

        dedup_key = f"{first_name.lower()}|{last_name.lower()}"
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        records.append(
            StudentRecord(
                full_name=f"{first_name} {last_name}",
                first_name=first_name,
                last_name=last_name,
                semester=semester_label,
                award=award,
            )
        )

        if max_names and len(records) >= max_names:
            break

    return records


@provider("cla")
def fetch_cla_names(
    semesters: list[str],
    max_names: Optional[int] = None,
    session: Optional[requests.Session] = None,
) -> dict[str, list[StudentRecord]]:
    """
    Scrape CLA Dean's List pages for the requested semesters.
    Returns dict mapping semester label -> list[StudentRecord].
    """
    sess = session or requests.Session()

    requested_labels = {SEMESTER_LABELS[s] for s in semesters if s in SEMESTER_LABELS}
    if not requested_labels:
        print("[cla] No valid semester codes provided.")
        return {}

    available = set(CLA_SEMESTER_URLS)
    per_semester: dict[str, list[StudentRecord]] = {}

    for label in sorted(requested_labels):
        if label not in available:
            print(
                f"[cla] {label}: no data available. "
                f"Supported: {', '.join(sorted(available))}"
            )
            continue

        url = CLA_SEMESTER_URLS[label]
        print(f"[cla] Fetching {label} from {url} …")

        try:
            resp = sess.get(url, timeout=60)
            resp.raise_for_status()
        except Exception as exc:
            print(f"[cla] Error fetching {label}: {exc}")
            continue

        records = _parse_cla_page(resp.text, label, max_names=max_names)
        if not records:
            print(f"[cla]   {label}: 0 names found (page may not exist or format changed).")
        else:
            per_semester[label] = records
            print(f"[cla]   {label}: {len(records)} unique names")

    return per_semester


# ---------------------------------------------------------------------------
# Agriculture provider
# ---------------------------------------------------------------------------

AGRICULTURE_BASE_URL = "https://ag.purdue.edu/department/oap/awards/students.php"

AGRICULTURE_SEMESTER_URLS: dict[str, str] = {
    "Fall 2025": f"{AGRICULTURE_BASE_URL}?list=fall2025#top",
    "Spring 2025": f"{AGRICULTURE_BASE_URL}?list=spring2025#top",
    "Fall 2024": f"{AGRICULTURE_BASE_URL}?list=fall2024#top",
    "Spring 2024": f"{AGRICULTURE_BASE_URL}?list=spring2024#top",
    "Fall 2023": f"{AGRICULTURE_BASE_URL}?list=fall2023#top",
    "Spring 2023": f"{AGRICULTURE_BASE_URL}?list=spring2023#top",
    "Fall 2022": f"{AGRICULTURE_BASE_URL}?list=fall2022#top",
}

_AGRICULTURE_SKIP_PATTERNS = [
    "back to top",
    "dean's list requirements",
    "semester honors requirements",
    "select a semester",
    "attain at least",
    "have at least",
]


def _parse_agriculture_page(
    html: str,
    semester_label: str,
    max_names: Optional[int] = None,
) -> list[StudentRecord]:
    """
    Parse student names from an Agriculture Dean's List page.

    Names appear in <strong> tags followed by major and award text.
    Format: <strong>FirstName LastName</strong>Major1Major2Award
    """
    soup = BeautifulSoup(html, "lxml")
    
    records: list[StudentRecord] = []
    seen: set[str] = set()

    # Find all <strong> tags which contain student names
    for strong_tag in soup.find_all("strong"):
        full_name = strong_tag.get_text(strip=True)
        
        # Skip if empty or looks like a header
        if not full_name:
            continue
        
        low = full_name.lower()
        if any(skip in low for skip in _AGRICULTURE_SKIP_PATTERNS):
            continue
        
        # Names should have at least 2 words
        parts = full_name.split()
        if len(parts) < 2:
            continue
        
        # Skip single letter entries (navigation)
        if len(full_name) <= 2:
            continue
            
        # Extract first and last name
        first_name = parts[0]
        last_name = parts[-1]
        
        # Get the text after the strong tag to find award info
        award = ""
        next_text = ""
        
        # Navigate siblings to find award text
        sibling = strong_tag.next_sibling
        while sibling and len(next_text) < 200:  # Limit search
            if isinstance(sibling, str):
                next_text += sibling
            elif hasattr(sibling, 'get_text'):
                next_text += sibling.get_text()
            sibling = sibling.next_sibling if sibling else None
            
            # Stop if we found award text
            if "Dean's List" in next_text or "Semester Honors" in next_text:
                break
        
        # Extract award from the text
        if "Dean's List & Semester Honors" in next_text:
            award = "Dean's List & Semester Honors"
        elif "Dean's List" in next_text:
            award = "Dean's List"
        elif "Semester Honors" in next_text:
            award = "Semester Honors"
        else:
            # No award found, skip this entry
            continue
        
        # Deduplicate
        dedup_key = f"{first_name.lower()}|{last_name.lower()}"
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        
        records.append(
            StudentRecord(
                full_name=full_name,
                first_name=first_name,
                last_name=last_name,
                semester=semester_label,
                award=award,
            )
        )
        
        if max_names and len(records) >= max_names:
            break
    
    return records


@provider("agriculture")
def fetch_agriculture_names(
    semesters: list[str],
    max_names: Optional[int] = None,
    session: Optional[requests.Session] = None,
) -> dict[str, list[StudentRecord]]:
    """
    Scrape Agriculture Dean's List pages for the requested semesters.
    Returns dict mapping semester label -> list[StudentRecord].
    """
    sess = session or requests.Session()

    requested_labels = {SEMESTER_LABELS[s] for s in semesters if s in SEMESTER_LABELS}
    if not requested_labels:
        print("[agriculture] No valid semester codes provided.")
        return {}

    available = set(AGRICULTURE_SEMESTER_URLS)
    per_semester: dict[str, list[StudentRecord]] = {}

    for label in sorted(requested_labels):
        if label not in available:
            print(
                f"[agriculture] {label}: no data available. "
                f"Supported: {', '.join(sorted(available))}"
            )
            continue

        url = AGRICULTURE_SEMESTER_URLS[label]
        print(f"[agriculture] Fetching {label} from {url} …")

        try:
            resp = sess.get(url, timeout=60)
            resp.raise_for_status()
        except Exception as exc:
            print(f"[agriculture] Error fetching {label}: {exc}")
            continue

        records = _parse_agriculture_page(resp.text, label, max_names=max_names)
        if not records:
            print(f"[agriculture]   {label}: 0 names found (page may not exist or format changed).")
        else:
            per_semester[label] = records
            print(f"[agriculture]   {label}: {len(records)} unique names")

    return per_semester


# ---------------------------------------------------------------------------
# Business provider
# ---------------------------------------------------------------------------

BUSINESS_BASE_URL = "https://business.purdue.edu/awards/students.php"

BUSINESS_SEMESTER_URLS: dict[str, str] = {
    "Fall 2025":   f"{BUSINESS_BASE_URL}?list=fall2025#top",
    "Spring 2025": f"{BUSINESS_BASE_URL}?list=spring2025#top",
    "Fall 2024":   f"{BUSINESS_BASE_URL}?list=fall2024#top",
    "Spring 2024": f"{BUSINESS_BASE_URL}?list=spring2024#top",
    "Fall 2023":   f"{BUSINESS_BASE_URL}?list=fall2023#top",
    "Spring 2023": f"{BUSINESS_BASE_URL}?list=spring2023#top",
    "Fall 2022":   f"{BUSINESS_BASE_URL}?list=fall2022#top",
}

_BUSINESS_SKIP_PATTERNS = [
    "back to top",
    "dean's list and semester honors",
    "select a semester",
    "attain at least",
    "have at least",
    "dean's list requirements",
    "semester honors requirements",
    "building the future",
]


def _parse_business_page(
    html: str,
    semester_label: str,
    max_names: Optional[int] = None,
) -> list[StudentRecord]:
    """
    Parse student names from a Business school Dean's List page.

    Names appear in <strong> tags inside <li> items.
    Format: <strong>FirstName LastName</strong>MajorAward
    Award is the last line of text in the list item:
    "Dean's List", "Semester Honors", or "Dean's List & Semester Honors".
    """
    soup = BeautifulSoup(html, "lxml")

    records: list[StudentRecord] = []
    seen: set[str] = set()

    for strong_tag in soup.find_all("strong"):
        full_name = strong_tag.get_text(strip=True)

        if not full_name:
            continue

        low = full_name.lower()
        if any(skip in low for skip in _BUSINESS_SKIP_PATTERNS):
            continue

        # Skip single-letter alphabetical navigation anchors
        if len(full_name) <= 2:
            continue

        parts = full_name.split()
        if len(parts) < 2:
            continue

        first_name = parts[0]
        last_name = parts[-1]

        # Gather sibling text to extract award info
        next_text = ""
        sibling = strong_tag.next_sibling
        while sibling and len(next_text) < 300:
            if isinstance(sibling, str):
                next_text += sibling
            elif hasattr(sibling, "get_text"):
                next_text += sibling.get_text()
            if "Dean's List" in next_text or "Semester Honors" in next_text:
                break
            sibling = sibling.next_sibling if sibling else None

        if "Dean's List & Semester Honors" in next_text:
            award = "Dean's List & Semester Honors"
        elif "Dean's List" in next_text:
            award = "Dean's List"
        elif "Semester Honors" in next_text:
            award = "Semester Honors"
        else:
            continue

        dedup_key = f"{first_name.lower()}|{last_name.lower()}"
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        records.append(
            StudentRecord(
                full_name=full_name,
                first_name=first_name,
                last_name=last_name,
                semester=semester_label,
                award=award,
            )
        )

        if max_names and len(records) >= max_names:
            break

    return records


@provider("business")
def fetch_business_names(
    semesters: list[str],
    max_names: Optional[int] = None,
    session: Optional[requests.Session] = None,
) -> dict[str, list[StudentRecord]]:
    """
    Scrape Business school Dean's List pages for the requested semesters.
    Returns dict mapping semester label -> list[StudentRecord].
    """
    sess = session or requests.Session()

    requested_labels = {SEMESTER_LABELS[s] for s in semesters if s in SEMESTER_LABELS}
    if not requested_labels:
        print("[business] No valid semester codes provided.")
        return {}

    available = set(BUSINESS_SEMESTER_URLS)
    per_semester: dict[str, list[StudentRecord]] = {}

    for label in sorted(requested_labels):
        if label not in available:
            print(
                f"[business] {label}: no data available. "
                f"Supported: {', '.join(sorted(available))}"
            )
            continue

        url = BUSINESS_SEMESTER_URLS[label]
        print(f"[business] Fetching {label} from {url} …")

        try:
            resp = sess.get(url, timeout=60)
            resp.raise_for_status()
        except Exception as exc:
            print(f"[business] Error fetching {label}: {exc}")
            continue

        records = _parse_business_page(resp.text, label, max_names=max_names)
        if not records:
            print(f"[business]   {label}: 0 names found (page may not exist or format changed).")
        else:
            per_semester[label] = records
            print(f"[business]   {label}: {len(records)} unique names")

    return per_semester
