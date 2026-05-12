"""
Transfermarkt player-season scraper.

Scrapes per-match performance rows (competition, matchday, date, venue,
result, goals, assists, cards, minutes, rating, …) for one or more
Transfermarkt player-season URLs and writes them to a CSV.

Usage:
    python transfermarkt_scraper.py -i urls.txt -o scraped_data.csv
    python transfermarkt_scraper.py --input urls.txt --output out.csv --concurrency 2
    python transfermarkt_scraper.py -i urls.txt --headed

Run `python transfermarkt_scraper.py --help` for the full list of options.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import random
import sys
from urllib.parse import urlparse, parse_qs

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ── defaults ──────────────────────────────────────────────────────────────────
# These are only DEFAULTS — every value below can be overridden from the CLI.

DEFAULT_INPUT_FILE       = "urls.txt"
DEFAULT_OUTPUT_FILE      = "scraped_data.csv"

# Lower these if you keep hitting CloudFront 403s. Start at 1/1 if your IP
# was recently blocked; raise once stable.
DEFAULT_CONCURRENCY      = 1
DEFAULT_CONTEXTS         = 1   # one context per concurrent tab → separate cookie jars

# Throttle between successful page loads (seconds). Randomized in [MIN, MAX].
DEFAULT_THROTTLE_MIN_SEC = 1.0
DEFAULT_THROTTLE_MAX_SEC = 2.5

# Seconds to wait after scrolling so the table has time to render.
DEFAULT_SCROLL_WAIT_SEC  = 3.0

# Per-URL retry budget for transient failures (CAPTCHA, slow loads, etc.)
DEFAULT_PER_URL_RETRIES  = 2

# Use real Chrome instead of bundled Chromium (much harder to fingerprint).
# Requires Chrome installed locally. Falls back to Chromium if launch fails.
DEFAULT_USE_CHROME       = True

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Headers a normal Chrome on Windows sends. Without these, CloudFront flags
# the request as automated.
EXTRA_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

CSV_HEADERS = [
    "source_url", "player_id", "player_slug", "season_id",
    "scraped_season_id", "scraped_season_label",
    "competition", "competition_url",
    "matchday", "matchday_url", "date", "venue",
    "home_team", "away_team", "result",
    "position", "home_team_url", "away_team_url", "result_url",
    "was_captain",
    "goals", "assists", "own_goals",
    "yellow_cards", "second_yellow", "red_cards",
    "subbed_on_min", "subbed_off_min",
    "rating", "minutes", "minutes_num",
    "row_note",
]

# CAPTCHA detection — Transfermarkt's "Let's confirm you are human" page
CAPTCHA_XPATH = "xpath=//h1[contains(.,'Let') and contains(.,'human')]"

# Transfermarkt's "No information for this season" placeholder.
NO_INFO_XPATH = "xpath=//div[contains(text(),'No information')]"

# ── JS extractor ──────────────────────────────────────────────────────────────

JS_EXTRACT = """
() => {
    const BASE = "https://www.transfermarkt.com";

    function abs(href) {
        if (!href) return "";
        if (href.startsWith("http")) return href;
        return BASE + href;
    }

    const NOTE_SELECTOR = "div.tm-grid__cell.no-border-right.svelte-dx2jdr";

    function getNote(row) {
        const el = row.querySelector(NOTE_SELECTOR);
        return el ? el.innerText.trim() : "";
    }

    const rowgroups = document.querySelectorAll('[role="table"] [role="rowgroup"]');
    const results   = [];

    for (let i = 1; i < rowgroups.length; i++) {
        const rg  = rowgroups[i];
        const box = rg.closest(".box");

        const compLink        = box ? box.querySelector(".content-box-headline a") : null;
        const competition     = compLink ? compLink.innerText.trim() : "";
        const competition_url = abs(compLink ? compLink.getAttribute("href") : "");

        const rows = rg.querySelectorAll('[role="row"]');

        for (const row of rows) {
            const divs    = row.querySelectorAll(":scope > div");
            const anchors = row.querySelectorAll(":scope > a");

            if (divs.length < 5) {
                const note = getNote(row) || (divs[0] ? divs[0].innerText.trim() : "");
                if (note) {
                    results.push({ row_note: note, competition, competition_url });
                }
                continue;
            }

            const d  = (n) => divs[n]    ? divs[n].innerText.trim()    : "";
            const a  = (n) => anchors[n] ? anchors[n].innerText.trim() : "";
            const ah = (n) => abs(anchors[n] ? anchors[n].getAttribute("href") : "");

            const htA  = divs[2] ? divs[2].querySelector("a") : null;
            const atA  = divs[3] ? divs[3].querySelector("a") : null;
            const posA = divs[4] ? divs[4].querySelector("a") : null;

            const minutes_raw = d(14);
            const minutes_num = minutes_raw.replace(/[^\\d]/g, "");

            results.push({
                competition,
                competition_url,
                matchday:       a(0),
                matchday_url:   ah(0),
                date:           d(0),
                venue:          d(1),
                home_team:      htA ? htA.innerText.trim() : "",
                home_team_url:  abs(htA ? htA.getAttribute("href") : ""),
                away_team:      atA ? atA.innerText.trim() : "",
                away_team_url:  abs(atA ? atA.getAttribute("href") : ""),
                result:         a(1),
                result_url:     ah(1),
                position:       posA ? posA.innerText.trim() : "",
                was_captain:    divs[4] && divs[4].querySelector('img[title="Captain"]') ? "TRUE" : "FALSE",
                goals:          d(5),
                assists:        d(6),
                own_goals:      d(7),
                yellow_cards:   d(8),
                second_yellow:  d(9),
                red_cards:      d(10),
                subbed_on_min:  d(11),
                subbed_off_min: d(12),
                rating:         d(13),
                minutes:        minutes_raw,
                minutes_num,
                row_note:       posA ? getNote(row) : (getNote(row) || d(15)),
            });
        }
    }
    return results;
}
"""

JS_SEASON_LABEL = """
() => {
    const el = document.querySelector('.tm-select-box[data-name="seasons-select"] button span');
    return el ? el.innerText.trim() : "";
}
"""

# ── URL loading & resume ──────────────────────────────────────────────────────

def load_urls(path: str) -> list[str]:
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Input file '{path}' not found. Create it with one URL per line, "
            f"or pass a different path with --input."
        )
    with open(path, "r", encoding="utf-8") as f:
        urls = [
            line.strip()
            for line in f
            if line.strip() and not line.strip().startswith("#")
        ]
    print(f"Loaded {len(urls)} URL(s) from {path}")
    return urls


def load_done_urls(output_file: str) -> set[str]:
    """Return the set of source_urls already present in the output CSV."""
    if not os.path.exists(output_file) or os.path.getsize(output_file) == 0:
        return set()
    done: set[str] = set()
    with open(output_file, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if "source_url" not in (reader.fieldnames or []):
            return set()
        for row in reader:
            url = (row.get("source_url") or "").strip()
            if url:
                done.add(url)
    return done


# ── helpers ───────────────────────────────────────────────────────────────────

def normalize_url(url: str) -> str:
    if not url.startswith("http"):
        url = "https://" + url

    parsed = urlparse(url)
    qs     = parse_qs(parsed.query)

    if "saison" in qs:
        season   = qs["saison"][0]
        new_path = parsed.path.rstrip("/") + f"/saison/{season}"
        url = parsed._replace(path=new_path, query="").geturl()

    return url


def parse_url_meta(url: str) -> dict:
    parsed = urlparse(url)
    parts  = parsed.path.strip("/").split("/")

    player_slug = parts[0] if len(parts) > 0 else ""
    player_id   = parts[3] if len(parts) > 3 else ""

    season_id = ""
    if len(parts) > 7 and parts[6] == "saison":
        season_id = parts[7]

    return {
        "player_slug": player_slug,
        "player_id":   player_id,
        "season_id":   season_id,
    }


async def accept_cookies(page, prefix: str, max_attempts: int = 6) -> None:
    for _ in range(max_attempts):
        for frame in page.frames:
            try:
                btn = frame.locator("xpath=//button[@aria-label='Accept & continue']").first
                if await btn.count() > 0:
                    await btn.click(timeout=3_000)
                    print(f"{prefix} Cookies accepted.")
                    await asyncio.sleep(0.4)
                    return
            except Exception:
                continue
        await asyncio.sleep(0.4)


# ── CAPTCHA visibility check ──────────────────────────────────────────────────

async def is_captcha_visible(page) -> bool:
    """Return True if Transfermarkt's 'confirm you are human' page is showing."""
    try:
        return await page.locator(CAPTCHA_XPATH).first.is_visible()
    except Exception:
        return False


# ── "No information" detector ─────────────────────────────────────────────────

async def has_no_information(page) -> bool:
    """Return True if Transfermarkt is showing the 'No information' placeholder
    (player has no data for this season)."""
    try:
        return await page.locator(NO_INFO_XPATH).first.count() > 0
    except Exception:
        return False


# ── scroll & wait ─────────────────────────────────────────────────────────────

async def scroll_and_wait(page, prefix: str, scroll_wait_sec: float) -> None:
    """Scroll to the bottom, wait a few seconds for the table to render,
    then scroll back to the top for stable extraction."""
    try:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(scroll_wait_sec)
        await page.evaluate("window.scrollTo(0, 0)")
    except Exception as e:
        print(f"{prefix} Scroll error: {e}")


# ── resource blocker ──────────────────────────────────────────────────────────

# IMPORTANT: don't block stylesheets — CloudFront's bot rules flag clients
# that fetch HTML but never request CSS. Images/fonts/media are safe to drop.
BLOCKED_TYPES = {"image", "font", "media"}

async def block_resources(route):
    if route.request.resource_type in BLOCKED_TYPES:
        await route.abort()
    else:
        await route.continue_()


# ── per-page scraper ──────────────────────────────────────────────────────────

async def fetch_data(
    context,
    url: str,
    semaphore: asyncio.Semaphore,
    writer,
    lock: asyncio.Lock,
    csv_file,
    idx: int,
    total: int,
    *,
    per_url_retries: int,
    throttle_min: float,
    throttle_max: float,
    scroll_wait_sec: float,
):
    async with semaphore:
        prefix = f"[{idx}/{total}]"
        print(f"{prefix} [START] {url}")

        for attempt in range(1, per_url_retries + 2):  # initial try + retries
            page = await context.new_page()
            try:
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                except PlaywrightTimeoutError:
                    print(f"{prefix} [GOTO-TIMEOUT] attempt {attempt}")

                # Simple CAPTCHA check — pause and wait for the user to solve it.
                if await is_captcha_visible(page):
                    print(f"{prefix} [CAPTCHA] Solve it in the browser, then press Enter…")
                    await asyncio.to_thread(input, "Press Enter to continue...")

                await accept_cookies(page, prefix, max_attempts=4)

                # Scroll to bottom, wait, then scroll back to top.
                await scroll_and_wait(page, prefix, scroll_wait_sec)

                scraped_season_label = await page.evaluate(JS_SEASON_LABEL)

                raw_rows = await page.evaluate(JS_EXTRACT)
                url_meta = parse_url_meta(url)

                records = []
                for raw in raw_rows:
                    record = {h: "" for h in CSV_HEADERS}
                    record.update(url_meta)
                    record.update({
                        "source_url":           url,
                        "scraped_season_id":    url_meta["season_id"],
                        "scraped_season_label": scraped_season_label,
                    })
                    record.update(raw)
                    records.append(record)

                if records:
                    async with lock:
                        for rec in records:
                            writer.writerow(rec)
                        csv_file.flush()
                    print(f"{prefix} [DONE]  {url} — {len(records)} row(s)")
                else:
                    # No rows extracted. Could be a real "No information" page
                    # (player didn't play that season at all) or an empty table.
                    # Either way, write a placeholder so resume skips it.
                    note = "No information" if await has_no_information(page) else "No rows"
                    url_meta = parse_url_meta(url)
                    record = {h: "" for h in CSV_HEADERS}
                    record.update(url_meta)
                    record.update({
                        "source_url":           url,
                        "scraped_season_id":    url_meta["season_id"],
                        "scraped_season_label": scraped_season_label,
                        "row_note":             note,
                    })
                    async with lock:
                        writer.writerow(record)
                        csv_file.flush()
                    print(f"{prefix} [{note.upper()}] - [{scraped_season_label}] {url}")

                # Polite delay between successful loads.
                await asyncio.sleep(random.uniform(throttle_min, throttle_max))
                return

            except PlaywrightTimeoutError:
                print(f"{prefix} [TIMEOUT] attempt {attempt} — {url}")
                if attempt > per_url_retries:
                    return
                await asyncio.sleep(1.0 + attempt)
            except Exception as e:
                print(f"{prefix} [ERROR]   attempt {attempt} — {url} — {e}")
                if attempt > per_url_retries:
                    return
                await asyncio.sleep(1.0 + attempt)
            finally:
                try:
                    await page.close()
                except Exception:
                    pass


# ── entry point ───────────────────────────────────────────────────────────────

async def run(args: argparse.Namespace) -> None:
    all_urls  = load_urls(args.input)
    done_urls = load_done_urls(args.output)

    all_urls = [normalize_url(u) for u in all_urls]
    pending  = [u for u in all_urls if u not in done_urls]
    skipped  = len(all_urls) - len(pending)

    if skipped:
        print(f"Resuming — skipping {skipped} URL(s) already present in {args.output}")
    if not pending:
        print(f"Nothing to scrape. All URLs are already in {args.output}.")
        return

    print(
        f"Scraping {len(pending)} URL(s) with {args.concurrency} concurrent tab(s) / "
        f"{args.contexts} context(s) …\n"
    )

    # Append if there is already content in the output file; otherwise (re)create.
    output_exists  = os.path.exists(args.output)
    output_is_zero = output_exists and os.path.getsize(args.output) == 0
    file_mode = "a" if (output_exists and not output_is_zero) else "w"

    semaphore = asyncio.Semaphore(args.concurrency)
    lock      = asyncio.Lock()

    with open(args.output, file_mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        if file_mode == "w":
            writer.writeheader()
            f.flush()

        async with async_playwright() as p:
            # Prefer real Chrome (channel="chrome"); fall back to bundled Chromium.
            browser = None
            headless = not args.headed
            if args.use_chrome:
                try:
                    browser = await p.chromium.launch(headless=headless, channel="chrome")
                    print("Launched real Chrome (channel='chrome').")
                except Exception as e:
                    print(f"Could not launch real Chrome ({e}); falling back to bundled Chromium.")
            if browser is None:
                browser = await p.chromium.launch(headless=headless)
                print("Launched bundled Chromium.")

            contexts = [
                await browser.new_context(
                    user_agent=USER_AGENT,
                    locale="en-US",
                    timezone_id="Europe/London",
                    viewport={"width": 1366, "height": 900},
                    extra_http_headers=EXTRA_HEADERS,
                )
                for _ in range(args.contexts)
            ]
            for ctx in contexts:
                await ctx.route("**/*", block_resources)

            tasks = [
                fetch_data(
                    contexts[idx % args.contexts],
                    url,
                    semaphore,
                    writer,
                    lock,
                    f,
                    idx + 1,
                    len(pending),
                    per_url_retries=args.retries,
                    throttle_min=args.throttle_min,
                    throttle_max=args.throttle_max,
                    scroll_wait_sec=args.scroll_wait,
                )
                for idx, url in enumerate(pending)
            ]
            await asyncio.gather(*tasks)
            await browser.close()

    print(f"\nDone. Results saved to {args.output}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="transfermarkt-scraper",
        description=(
            "Scrape per-match performance data for Transfermarkt player-season URLs.\n"
            "Reads URLs from a plain-text file (one per line) and writes results to CSV."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python transfermarkt_scraper.py -i urls.txt\n"
            "  python transfermarkt_scraper.py -i urls.txt -o output.csv -c 2\n"
            "  python transfermarkt_scraper.py -i urls.txt --headed --no-chrome\n"
        ),
    )

    # I/O
    parser.add_argument(
        "-i", "--input",
        default=DEFAULT_INPUT_FILE,
        help=f"Path to text file with one URL per line (default: {DEFAULT_INPUT_FILE}).",
    )
    parser.add_argument(
        "-o", "--output",
        default=DEFAULT_OUTPUT_FILE,
        help=f"Path to output CSV file (default: {DEFAULT_OUTPUT_FILE}).",
    )

    # Concurrency
    parser.add_argument(
        "-c", "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Number of concurrent tabs (default: {DEFAULT_CONCURRENCY}).",
    )
    parser.add_argument(
        "--contexts",
        type=int,
        default=DEFAULT_CONTEXTS,
        help=f"Number of browser contexts / cookie jars (default: {DEFAULT_CONTEXTS}).",
    )

    # Throttle & retries
    parser.add_argument(
        "--throttle-min",
        type=float,
        default=DEFAULT_THROTTLE_MIN_SEC,
        help=f"Min seconds to wait between successful loads (default: {DEFAULT_THROTTLE_MIN_SEC}).",
    )
    parser.add_argument(
        "--throttle-max",
        type=float,
        default=DEFAULT_THROTTLE_MAX_SEC,
        help=f"Max seconds to wait between successful loads (default: {DEFAULT_THROTTLE_MAX_SEC}).",
    )
    parser.add_argument(
        "--scroll-wait",
        type=float,
        default=DEFAULT_SCROLL_WAIT_SEC,
        help=f"Seconds to wait after scrolling for the table to render (default: {DEFAULT_SCROLL_WAIT_SEC}).",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_PER_URL_RETRIES,
        help=f"Per-URL retry budget for transient failures (default: {DEFAULT_PER_URL_RETRIES}).",
    )

    # Browser
    chrome_group = parser.add_mutually_exclusive_group()
    chrome_group.add_argument(
        "--use-chrome",
        dest="use_chrome",
        action="store_true",
        default=DEFAULT_USE_CHROME,
        help="Use real Chrome (channel='chrome'). Default: enabled.",
    )
    chrome_group.add_argument(
        "--no-chrome",
        dest="use_chrome",
        action="store_false",
        help="Use bundled Chromium instead of real Chrome.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser window (default: headless).",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.concurrency < 1:
        parser.error("--concurrency must be >= 1")
    if args.contexts < 1:
        parser.error("--contexts must be >= 1")
    if args.throttle_min < 0 or args.throttle_max < 0:
        parser.error("--throttle-min / --throttle-max must be >= 0")
    if args.throttle_max < args.throttle_min:
        parser.error("--throttle-max must be >= --throttle-min")

    try:
        asyncio.run(run(args))
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
