# Purdue Dean's List → Email Scraper

Scrapes student names from the Purdue College of Science Dean's List / Semester Honors page, looks up each name in the Purdue Directory to find their alias, and writes results to one CSV per semester.

## Setup

```bash
# Create a virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# (Optional) Install Playwright browser for fallback lookups
playwright install chromium
```

## Usage

```bash
# Default: Spring 2025 + Fall 2022 + Spring 2023 (8 parallel workers)
python scraper.py

# Specific semester(s)
python scraper.py --semester 202610              # Fall 2025 only
python scraper.py --semester 202520 202310       # Spring 2025 + Fall 2022

# Label for a different college (changes CSV filename prefix)
python scraper.py --college engineering

# Speed tuning
python scraper.py --workers 12 --delay 0.05      # More threads, less delay
python scraper.py --workers 1 --delay 0.3         # Conservative / sequential

# Limit names per semester (testing)
python scraper.py --max-names 20

# Skip Playwright fallback
python scraper.py --no-browser
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

Each semester gets its own CSV in `output/`, named `{college}_emails_{semester}.csv`.  
For example: `science_emails_fall_2025.csv`, `science_emails_spring_2023.csv`.

| Column   | Description |
|----------|-------------|
| name     | Full name from honors page |
| semester | Which semester the student appeared in |
| alias    | Purdue directory alias |
| email    | alias@purdue.edu |
| status   | `matched`, `unmatched`, `error_request` |

## Troubleshooting

- **No names found**: Check you have internet access and the semester code is valid.
- **Many "unmatched"**: Some students may have graduated and been removed from the directory. Try a more recent semester.
- **Slow**: Increase `--workers` (default 8) and decrease `--delay` (default 0.1s).
- **Playwright errors**: Run `playwright install chromium` or use `--no-browser` to skip.
