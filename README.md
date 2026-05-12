# Transfermarkt Player-Season Scraper

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Playwright](https://img.shields.io/badge/Playwright-Async-2EAD33?logo=playwright&logoColor=white)](https://playwright.dev/python/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Code style: PEP8](https://img.shields.io/badge/code%20style-PEP8-informational.svg)](https://peps.python.org/pep-0008/)

A fast, resilient async scraper for **[Transfermarkt](https://www.transfermarkt.com/)** player-season pages. Give it a list of URLs, get back a clean CSV with every match the player played that season — competition, matchday, date, venue, opponent, result, goals, assists, cards, minutes, rating, captaincy, and more.

Built with **Playwright (async)** and designed to survive CloudFront's anti-bot rules, CAPTCHAs, slow loads, and partial runs.

---

## Features

- **CLI-driven** — point it at any URL list and any output file, no code edits.
- **Resume support** — re-running skips URLs already in the output CSV.
- **CAPTCHA-aware** — pauses for manual solve and resumes automatically.
- **Anti-bot hardened** — real Chrome channel, realistic headers, CSS preserved, only images/fonts/media blocked.
- **Resilient** — per-URL retries with backoff, polite randomized throttling, handles "No information" pages.
- **Configurable concurrency** — tabs and cookie-jar contexts independent.
- **Schema-stable output** — 32-column CSV ready for pandas / SQL.

---

## Quickstart

```bash
# 1. Clone
git clone https://github.com/<your-username>/transfermarkt-scraper.git
cd transfermarkt-scraper

# 2. Install
pip install -r requirements.txt
playwright install chromium

# 3. Add URLs (one per line) to urls.txt — a sample is included

# 4. Run
python transfermarkt_scraper.py -i urls.txt -o scraped_data.csv
```

That's it. The CSV is created with headers on the first run and appended to on resume.

---

## Usage

```text
python transfermarkt_scraper.py [-h] [-i INPUT] [-o OUTPUT]
                                [-c CONCURRENCY] [--contexts CONTEXTS]
                                [--throttle-min THROTTLE_MIN]
                                [--throttle-max THROTTLE_MAX]
                                [--scroll-wait SCROLL_WAIT]
                                [--retries RETRIES]
                                [--use-chrome | --no-chrome] [--headed]
```

### Examples

```bash
# Default: reads urls.txt, writes scraped_data.csv, 1 tab, headless Chrome
python transfermarkt_scraper.py

# Custom input/output
python transfermarkt_scraper.py -i my_urls.txt -o output/players.csv

# Two concurrent tabs across two cookie-jar contexts
python transfermarkt_scraper.py -i urls.txt -c 2 --contexts 2

# Watch the browser work (useful for debugging or solving CAPTCHAs)
python transfermarkt_scraper.py -i urls.txt --headed

# Force bundled Chromium instead of real Chrome
python transfermarkt_scraper.py -i urls.txt --no-chrome

# Aggressive throttle if you're getting blocked
python transfermarkt_scraper.py -i urls.txt --throttle-min 3 --throttle-max 6 --retries 4
```

### All options

| Flag | Default | Description |
| --- | --- | --- |
| `-i, --input` | `urls.txt` | Text file with one URL per line. `#` starts a comment. |
| `-o, --output` | `scraped_data.csv` | Output CSV path. Appends if it exists. |
| `-c, --concurrency` | `1` | Number of concurrent tabs. |
| `--contexts` | `1` | Number of browser contexts (separate cookie jars). |
| `--throttle-min` | `1.0` | Min seconds between successful loads. |
| `--throttle-max` | `2.5` | Max seconds between successful loads. |
| `--scroll-wait` | `3.0` | Seconds to wait after scrolling for the table to render. |
| `--retries` | `2` | Per-URL retry budget for transient failures. |
| `--use-chrome` / `--no-chrome` | `--use-chrome` | Use real Chrome (`channel='chrome'`) or bundled Chromium. |
| `--headed` | off | Show the browser window. |

---

## Input format

A plain text file with one Transfermarkt player-season URL per line. Blank lines and `#` comments are ignored.

```text
# Erling Haaland — 2022/23
https://www.transfermarkt.com/erling-haaland/leistungsdaten/spieler/418560/saison/2022

# Lionel Messi — 2011/12
https://www.transfermarkt.com/lionel-messi/leistungsdaten/spieler/28003/saison/2011
```

Both `?saison=YYYY` query-string and `/saison/YYYY` path-style URLs are accepted — the scraper normalizes them automatically.

---

## Output schema

A 32-column CSV. Headers are written once; resume runs append.

| Column | Description |
| --- | --- |
| `source_url` | The URL that produced this row. Used for resume. |
| `player_id`, `player_slug` | Parsed from the URL. |
| `season_id`, `scraped_season_id`, `scraped_season_label` | Season from URL vs. season shown on page. |
| `competition`, `competition_url` | Competition name and Transfermarkt URL. |
| `matchday`, `matchday_url`, `date`, `venue` | Match metadata. |
| `home_team`, `home_team_url`, `away_team`, `away_team_url` | Both clubs. |
| `result`, `result_url` | Final score and match-report URL. |
| `position`, `was_captain` | Player's role in the match. |
| `goals`, `assists`, `own_goals` | Attacking output. |
| `yellow_cards`, `second_yellow`, `red_cards` | Disciplinary record. |
| `subbed_on_min`, `subbed_off_min` | Substitution minutes. |
| `rating`, `minutes`, `minutes_num` | Performance numbers. `minutes_num` is digits-only. |
| `row_note` | Free-text notes (e.g. "Not in squad", "No information"). |

---

## How it stays out of trouble

Transfermarkt sits behind CloudFront, which is aggressive about bot detection. This scraper:

- Launches **real Chrome** when available (much harder to fingerprint than bundled Chromium).
- Sends the full set of `Sec-Ch-Ua`, `Sec-Fetch-*`, and `Accept-*` headers a real Chrome on Windows would send.
- **Does not block stylesheets** — CloudFront flags HTML-only fetchers. Only images, fonts, and media are blocked.
- Uses a polite **randomized throttle** between successful loads.
- **Pauses on CAPTCHA** so you can solve it once and continue.
- **Retries** transient failures with linear backoff.

If you're still getting 403s, drop concurrency to 1 and raise `--throttle-min`/`--throttle-max`.

---

## Project layout

```
transfermarkt-scraper/
├── transfermarkt_scraper.py   # main scraper (CLI entry point)
├── urls.txt                   # sample input — replace with your own
├── requirements.txt
├── .gitignore
├── LICENSE
└── README.md
```

---

## Requirements

- Python 3.10+
- Google Chrome installed (optional but recommended) — falls back to bundled Chromium with `--no-chrome`
- Packages: `playwright`

Install:

```bash
pip install -r requirements.txt
playwright install chromium
```

---

## Notes & caveats

- This project is for **educational and personal research use**. Respect Transfermarkt's [Terms of Use](https://www.transfermarkt.com/intern/anb) and `robots.txt`, and don't hammer the site.
- Output is best-effort — Transfermarkt occasionally changes its DOM. If a column starts coming back empty, the selectors in `JS_EXTRACT` are the first place to look.
- CAPTCHA solving requires the browser to be visible. Run with `--headed` if you expect to hit one.

---

## License

[MIT](LICENSE) — free to use, modify, and distribute.
