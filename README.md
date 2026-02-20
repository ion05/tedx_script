# Purdue Dean's List → Email Scraper

Scrapes student names from Purdue college honors / Dean's List pages, looks up each name in the Purdue Directory to find their alias, and writes results to one CSV per semester.

Currently supports **College of Science**, **College of Engineering**, **Polytechnic**, **College of Liberal Arts (CLA)**, **College of Agriculture**, and **Mitch Daniels School of Business**. Adding more colleges is straightforward — just add a new provider function in `name_sources.py`.

## Setup

```bash
# Create a virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

## Usage

```bash
# Default: Science, Spring 2025 + Fall 2022 + Spring 2023
python scraper.py

# Engineering — Fall 2025 through Fall 2022 (all terms in between)
python scraper.py --college engineering \
    --semester 202610 202520 202510 202420 202410 202320 202310

# CLA (Liberal Arts) — Fall 2025 through Fall 2022
python scraper.py --college cla \
    --semester 202610 202520 202510 202420 202410 202310

# Agriculture — Fall 2025 through Fall 2022
python scraper.py --college agriculture \
    --semester 202610 202520 202510 202420 202410 202320 202310

# Business — Fall 2025 through Fall 2022
python scraper.py --college business \
    --semester 202610 202520 202510 202420 202410 202320 202310

# Specific semester(s)
python scraper.py --semester 202610              # Fall 2025 only
python scraper.py --semester 202520 202310       # Spring 2025 + Fall 2022

# Speed tuning
python scraper.py --workers 12 --delay 0.05      # More threads, less delay
python scraper.py --workers 1 --delay 0.3         # Conservative / sequential

# Limit names per semester (testing)
python scraper.py --max-names 20
```

### Semester Codes

| Code   | Semester      |
|--------|---------------|
| 202610 | Fall 2025     |
| 202530 | Summer 2025   |
| 202520 | Spring 2025   |
| 202510 | Fall 2024     |
| 202420 | Spring 2024   |
| 202410 | Fall 2023     |
| 202320 | Spring 2023   |
| 202310 | Fall 2022     |

## Output

Each semester gets its own CSV. The output location depends on the college:

| College     | Directory                |
|-------------|--------------------------|
| science     | `output/`                |
| engineering | `output/engineering/`    |
| polytechnic | `output/polytechnic/`    |
| cla         | `output/cla/`            |
| agriculture | `output/agriculture/`    |
| business    | `output/business/`       |

Files are named `{college}_emails_{semester}.csv`.  
For example: `engineering_emails_fall_2025.csv`, `science_emails_spring_2023.csv`.

When multiple semesters are processed, a combined unique-email CSV is also generated (e.g. `engineering_all_unique_emails.csv`).

| Column   | Description |
|----------|-------------|
| name     | Full name from honors page |
| semester | Which semester the student appeared in |
| alias    | Purdue directory alias |
| email    | alias@purdue.edu |
| status   | `matched`, `unmatched`, `error_request` |

## Architecture

- **`name_sources.py`** — College-specific providers. Each one fetches student names and returns `dict[semester_label, list[StudentRecord]]`. Adding a new college means adding a new `@provider("college_name")` function here.
- **`scraper.py`** — Shared pipeline: Purdue Directory lookup, concurrent workers, CSV writing, CLI.

## Troubleshooting

- **No names found**: Check you have internet access and the semester code is valid.
- **Many "unmatched"**: Some students may have graduated and been removed from the directory. Try a more recent semester.
- **Slow**: Increase `--workers` (default 8) and decrease `--delay` (default 0.1s).
- **CSV not updating**: CSV is saved every 50 lookups automatically. If the script crashes, you'll still have partial results.
