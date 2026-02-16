# Purdue Dean's List → Email Scraper

Scrapes student names from the Purdue College of Science Dean's List / Semester Honors page, looks up each name in the Purdue Directory to find their alias, and writes the results to a CSV.

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
# Default: Spring 2025 + Fall 2022 + Spring 2023, all names
python scraper.py

# Limit to 20 names (good for testing)
python scraper.py --max-names 20

# Specific semester(s)
python scraper.py --semester 202510              # Fall 2024 only
python scraper.py --semester 202520 202310       # Spring 2025 + Fall 2022

# Faster lookups (less polite)
python scraper.py --delay 0.1

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

Results are written to `output/emails.csv` with these columns:

| Column | Description |
|--------|-------------|
| name     | Full name from honors page |
| semester | Which semester the student appeared in |
| alias    | Purdue directory alias |
| email    | alias@purdue.edu |
| status   | `matched`, `ambiguous`, `matched_fuzzy`, `matched_browser`, `unmatched`, `error_request` |

## Troubleshooting

- **No names found**: Check you have internet access and the semester code is valid.
- **Many "unmatched"**: Some students may have graduated and been removed from the directory. Try a more recent semester.
- **Slow**: The script is intentionally throttled. Reduce `--delay` for faster runs.
- **Playwright errors**: Run `playwright install chromium` or use `--no-browser` to skip.
