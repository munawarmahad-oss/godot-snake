#!/usr/bin/env python3
"""
car_deal_finder.py
------------------
Scrapes Facebook Marketplace for used car listings, cross-references each
against AutoTempest.com's Price Trends feature (/price-trends) to compute a
median market price from both current and historical listings, and flags
listings that are >= min_discount % below market as flip candidates.

Usage:
    python car_deal_finder.py --make toyota --model camry \
        --min-year 2018 --max-year 2022 --max-price 18000

Install deps:
    pip install -r requirements.txt
    playwright install chromium
"""

# --- IMPORTS & CONSTANTS ---

import argparse
import asyncio
import csv
import datetime
import getpass
import json
import math
import os
import pathlib
import re
import statistics
import sys
import time
import random
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlencode

try:
    from playwright.async_api import async_playwright, BrowserContext, Page
    from bs4 import BeautifulSoup
    import pgeocode
    from rich.console import Console
    from rich.table import Table
    from rich import box
except ImportError as e:
    print(f"[ERROR] Missing dependency: {e}")
    print("Run: python -m pip install -r requirements.txt && python -m playwright install chromium")
    sys.exit(1)

# playwright-stealth v1 uses stealth_async(); v2 uses Stealth().apply_stealth_async()
# This wrapper handles both versions transparently.
try:
    from playwright_stealth import stealth_async as _stealth_v1
    async def _apply_stealth(page):
        await _stealth_v1(page)
except ImportError:
    try:
        from playwright_stealth import Stealth as _Stealth
        _stealth_instance = _Stealth()
        async def _apply_stealth(page):
            await _stealth_instance.apply_stealth_async(page)
    except Exception:
        # stealth unavailable entirely — continue without it
        async def _apply_stealth(page):
            pass


FB_LOGIN_URL = "https://www.facebook.com/login"
FB_MARKETPLACE_BASE = "https://www.facebook.com/marketplace/category/vehicles"
AT_PRICE_TRENDS_BASE = "https://www.autotempest.com/price-trends"

DEFAULT_DISCOUNT_THRESHOLD = 25   # % below market to flag as deal
DEFAULT_DAYS_LISTED = 7           # only listings posted within this many days
DEFAULT_MAX_LISTINGS = 50
DEFAULT_RADIUS = 70               # miles
DEFAULT_ZIP = "48174"             # Romulus, MI

SCROLL_PAUSE_RANGE = (1.5, 3.5)   # seconds between scroll events
REQUEST_DELAY_RANGE = (2.0, 4.0)  # seconds between AT page fetches
AT_RETRY_DELAYS = [2, 4, 8]       # exponential backoff for AT 403s

console = Console()


# --- DATACLASSES ---

@dataclass
class FBListing:
    id: str
    title: str
    price: Optional[int]
    year: Optional[int]
    mileage: Optional[int]
    url: str
    location: str
    posted_timestamp: Optional[int] = None   # unix timestamp from FB GraphQL


@dataclass
class DealResult:
    listing: FBListing
    market_avg: Optional[float]
    at_sample_count: int
    discount_pct: Optional[float]   # negative = below market (good), positive = above
    is_deal: bool


# --- CONFIG & CREDENTIALS ---

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find flippable car deals on Facebook Marketplace using AutoTempest market data.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    req = parser.add_argument_group("required search parameters")
    req.add_argument("--make", required=True, help="Vehicle make, e.g. toyota")
    req.add_argument("--model", required=True, help="Vehicle model, e.g. camry")
    req.add_argument("--min-year", required=True, type=int, help="Minimum model year")
    req.add_argument("--max-year", required=True, type=int, help="Maximum model year")
    req.add_argument("--max-price", required=True, type=int, help="Maximum listing price in USD")

    parser.add_argument("--zip", default=DEFAULT_ZIP, help="ZIP code for search center")
    parser.add_argument("--radius", default=DEFAULT_RADIUS, type=int, help="Search radius in miles")
    parser.add_argument(
        "--min-discount", default=DEFAULT_DISCOUNT_THRESHOLD, type=float,
        help="Minimum %% below market to flag as a deal",
    )
    parser.add_argument(
        "--days-listed", default=DEFAULT_DAYS_LISTED, type=int,
        help="Only include listings posted within this many days",
    )
    parser.add_argument(
        "--max-listings", default=DEFAULT_MAX_LISTINGS, type=int,
        help="Maximum number of FB listings to process",
    )
    parser.add_argument(
        "--output", default=None,
        help="CSV output path (default: car_deals_TIMESTAMP.csv)",
    )
    parser.add_argument(
        "--headless", action=argparse.BooleanOptionalAction, default=True,
        help="Run browser headlessly (use --no-headless to watch)",
    )
    parser.add_argument(
        "--session-file", default=".fb_session.json",
        help="Playwright storage state file for persisting FB login",
    )
    parser.add_argument(
        "--cache-file", default=".at_cache.json",
        help="JSON file for caching AutoTempest price lookups between runs",
    )

    return parser.parse_args()


def get_fb_credentials(args: argparse.Namespace) -> tuple[str, str]:
    email = os.environ.get("FB_EMAIL", "").strip()
    password = os.environ.get("FB_PASSWORD", "").strip()
    if not email:
        email = input("Facebook email: ").strip()
    if not password:
        password = getpass.getpass("Facebook password: ")
    return email, password


def load_cache(path: str) -> dict:
    p = pathlib.Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_cache(path: str, data: dict) -> None:
    try:
        pathlib.Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        console.print(f"[yellow]Warning: could not save cache to {path}: {e}[/yellow]")


# --- BROWSER UTILITIES ---

async def build_browser_context(playwright, args: argparse.Namespace):
    """Launch Chromium with stealth settings and restore FB session if available."""
    browser = await playwright.chromium.launch(
        headless=args.headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ],
    )

    session_path = pathlib.Path(args.session_file)
    if session_path.exists():
        try:
            context = await browser.new_context(
                storage_state=str(session_path),
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            console.print(f"[dim]Loaded FB session from {session_path}[/dim]")
            return browser, context
        except Exception:
            pass  # corrupted state file — fall through to fresh context

    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    )
    return browser, context


async def save_session(context: BrowserContext, path: str) -> None:
    try:
        await context.storage_state(path=path)
        console.print(f"[dim]FB session saved to {path}[/dim]")
    except Exception as e:
        console.print(f"[yellow]Warning: could not save session: {e}[/yellow]")


# --- FACEBOOK MARKETPLACE SCRAPER ---

def zip_to_latlon(zip_code: str) -> tuple[float, float]:
    """Convert US ZIP code to lat/lon using pgeocode (fully offline)."""
    nomi = pgeocode.Nominatim("us")
    result = nomi.query_postal_code(zip_code)
    lat = result.latitude
    lon = result.longitude
    if math.isnan(lat) or math.isnan(lon):
        raise ValueError(f"Could not geocode ZIP code: {zip_code}")
    return float(lat), float(lon)


def build_fb_search_url(lat: float, lon: float, args: argparse.Namespace) -> str:
    query = f"{args.min_year} {args.make} {args.model}"
    params = {
        "query": query,
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "radius": args.radius,
        "daysSinceListed": args.days_listed,
        "sortBy": "creation_time_descend",
    }
    return FB_MARKETPLACE_BASE + "?" + urlencode(params)


async def fb_login(page: Page, email: str, password: str) -> None:
    """Log into Facebook. Handles cookie banners, slow loads, and 2FA."""
    console.print("[cyan]Logging into Facebook...[/cyan]")

    await page.goto(FB_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)

    # Give the page extra time to settle — Facebook sometimes renders slowly
    await asyncio.sleep(random.uniform(2.0, 3.5))

    # Dismiss cookie / consent banners that can block the login form.
    # Facebook uses several different banners depending on region.
    cookie_selectors = [
        "[data-cookiebanner='accept_button']",
        "button[title='Accept all']",
        "button[title='Allow all cookies']",
        "[data-testid='cookie-policy-manage-dialog-accept-button']",
        "button[data-testid='royal_login_button']",   # sometimes overlaps
        "div[aria-label='Allow all cookies'] button",
        "button:has-text('Accept All')",
        "button:has-text('Allow')",
    ]
    for sel in cookie_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                await asyncio.sleep(1.0)
                break
        except Exception:
            continue

    # Try multiple selectors for the email field — Facebook occasionally
    # restructures the login form between regions/experiments.
    email_selectors = ["#email", "input[name='email']", "input[type='email']"]
    email_found = False
    for sel in email_selectors:
        try:
            await page.wait_for_selector(sel, timeout=15000)
            email_found = True
            break
        except Exception:
            continue

    if not email_found:
        if "facebook.com/login" not in page.url and "facebook.com" in page.url:
            console.print("[dim]Already logged in — continuing.[/dim]")
            return
        raise RuntimeError(
            "Facebook login page did not load. "
            "Check your internet connection and try again with --no-headless "
            "to watch what the browser is doing."
        )

    # Fill email
    for sel in email_selectors:
        try:
            await page.fill(sel, email)
            break
        except Exception:
            continue

    await asyncio.sleep(random.uniform(0.4, 0.8))

    # Fill password
    pass_selectors = ["#pass", "input[name='pass']", "input[type='password']"]
    for sel in pass_selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=3000):
                await el.fill(password)
                break
        except Exception:
            continue

    await asyncio.sleep(random.uniform(0.4, 0.8))

    # Click login button
    login_selectors = [
        "button[name='login']",
        "button[type='submit']",
        "[data-testid='royal_login_button']",
        "input[type='submit'][value='Log In']",
    ]
    for sel in login_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=3000):
                await btn.click()
                break
        except Exception:
            continue

    # Wait for navigation away from the login page
    try:
        await page.wait_for_function(
            "() => !window.location.href.includes('/login')",
            timeout=20000,
        )
    except Exception:
        pass

    await asyncio.sleep(2.0)

    # Handle 2FA / checkpoint
    if "checkpoint" in page.url or "two_step" in page.url:
        console.print("[yellow]Facebook is asking for a verification code.[/yellow]")
        try:
            await page.wait_for_selector(
                "input[name='approvals_code'], input[id*='approvals']",
                timeout=8000,
            )
            code = input("Enter the Facebook verification code sent to your phone: ").strip()
            await page.fill("input[name='approvals_code']", code)
            # Try multiple submit button selectors
            for sel in ["#checkpointSubmitButton", "button[type='submit']"]:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=2000):
                        await btn.click()
                        break
                except Exception:
                    continue
            await asyncio.sleep(4.0)
        except Exception:
            pass

    # Final check — make sure we are no longer on login/checkpoint
    current = page.url
    if "/login" in current or "/checkpoint" in current:
        raise RuntimeError(
            "Facebook login failed. Make sure FB_EMAIL and FB_PASSWORD are correct.\n"
            "Run with --no-headless to watch the browser and see what went wrong."
        )

    console.print("[green]Facebook login successful.[/green]")


def find_feed_units(data) -> Optional[list]:
    """
    Recursively walk a nested dict/list to find the 'feed_units' key.
    Facebook's GraphQL response schema reshuffles the top-level namespace
    occasionally — this makes the parser resilient to those changes.
    """
    if isinstance(data, dict):
        if "feed_units" in data:
            edges = data["feed_units"].get("edges") or []
            return edges
        for value in data.values():
            result = find_feed_units(value)
            if result is not None:
                return result
    elif isinstance(data, list):
        for item in data:
            result = find_feed_units(item)
            if result is not None:
                return result
    return None


def _extract_int_price(formatted: str) -> Optional[int]:
    cleaned = re.sub(r"[^\d]", "", formatted or "")
    return int(cleaned) if cleaned else None


def _extract_year_from_subtitles(subtitles: list[str]) -> Optional[int]:
    for subtitle in subtitles:
        m = re.search(r"\b(19[5-9]\d|20[0-2]\d)\b", subtitle)
        if m:
            return int(m.group(1))
    return None


def _extract_mileage_from_subtitles(subtitles: list[str]) -> Optional[int]:
    for subtitle in subtitles:
        m = re.search(r"([\d,]+)\s*(?:mi(?:les?)?)", subtitle, re.IGNORECASE)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return None


def parse_fb_response(json_body: dict) -> list[FBListing]:
    """Parse a single intercepted /api/graphql/ JSON response into FBListings."""
    edges = find_feed_units(json_body)
    if not edges:
        return []

    listings = []
    for edge in edges:
        try:
            node = edge.get("node") or {}
            listing_data = node.get("listing") or {}
            if not listing_data:
                # Some edges wrap differently
                listing_data = node.get("marketplace_listing") or {}
            if not listing_data:
                continue

            listing_id = listing_data.get("id") or listing_data.get("listing_id")
            if not listing_id:
                continue

            title = listing_data.get("marketplace_listing_title", "")

            price_obj = listing_data.get("listing_price") or {}
            price = _extract_int_price(price_obj.get("formatted_amount", ""))

            # Creation time for recency filtering
            posted_ts = listing_data.get("creation_time")

            # Location
            loc_obj = (
                listing_data.get("location") or
                listing_data.get("marketplace_listing_seller") or {}
            )
            location_str = ""
            try:
                location_str = (
                    loc_obj.get("reverse_geocode", {})
                           .get("city_page", {})
                           .get("display_name", "")
                )
            except AttributeError:
                pass

            # Subtitles contain year, mileage, transmission etc.
            raw_subtitles = listing_data.get(
                "custom_sub_titles_with_rendering_flags", []
            ) or []
            subtitles = []
            for s in raw_subtitles:
                if isinstance(s, dict):
                    subtitles.append(s.get("subtitle", ""))
                elif isinstance(s, str):
                    subtitles.append(s)

            year = _extract_year_from_subtitles(subtitles)
            mileage = _extract_mileage_from_subtitles(subtitles)

            url = f"https://www.facebook.com/marketplace/item/{listing_id}/"

            listings.append(FBListing(
                id=str(listing_id),
                title=title,
                price=price,
                year=year,
                mileage=mileage,
                url=url,
                location=location_str,
                posted_timestamp=posted_ts,
            ))
        except Exception:
            continue

    return listings


def filter_fb_listings(
    listings: list[FBListing],
    args: argparse.Namespace,
) -> list[FBListing]:
    """Deduplicate, apply year range, max price, and recency filters."""
    seen_ids: set[str] = set()
    cutoff_ts = int(time.time()) - (args.days_listed * 86400)
    filtered = []

    for listing in listings:
        if listing.id in seen_ids:
            continue
        seen_ids.add(listing.id)

        if listing.price is not None and listing.price > args.max_price:
            continue

        if listing.year is not None:
            if listing.year < args.min_year or listing.year > args.max_year:
                continue

        # Recency filter — skip if creation_time is known and too old
        if listing.posted_timestamp is not None:
            if int(listing.posted_timestamp) < cutoff_ts:
                continue

        filtered.append(listing)

    return filtered


async def parse_fb_dom_fallback(page: Page, args: argparse.Namespace) -> list[FBListing]:
    """
    Fallback DOM scraper for when GraphQL interception yields no results.
    Extracts text from listing cards and uses regex to parse fields.
    """
    console.print("[yellow]GraphQL fallback: trying DOM scraping...[/yellow]")
    listings = []
    try:
        await page.wait_for_selector(
            '[data-testid="marketplace-listing-card"]', timeout=10000
        )
        cards = await page.query_selector_all(
            '[data-testid="marketplace-listing-card"]'
        )
    except Exception:
        return listings

    for card in cards:
        try:
            text = await card.inner_text()
            lines = [l.strip() for l in text.splitlines() if l.strip()]

            price = None
            year = None
            mileage = None
            title = lines[0] if lines else "Unknown"

            for line in lines:
                if price is None:
                    m = re.search(r"\$\s*([\d,]+)", line)
                    if m:
                        price = int(m.group(1).replace(",", ""))
                if year is None:
                    m = re.search(r"\b(19[5-9]\d|20[0-2]\d)\b", line)
                    if m:
                        year = int(m.group(1))
                if mileage is None:
                    m = re.search(r"([\d,]+)\s*(?:mi(?:les?)?)", line, re.IGNORECASE)
                    if m:
                        mileage = int(m.group(1).replace(",", ""))

            # Try to get link from card
            link_el = await card.query_selector("a")
            href = await link_el.get_attribute("href") if link_el else ""
            id_match = re.search(r"/item/(\d+)/", href or "")
            listing_id = id_match.group(1) if id_match else str(hash(title))
            url = f"https://www.facebook.com/marketplace/item/{listing_id}/" if id_match else ""

            listings.append(FBListing(
                id=listing_id,
                title=title,
                price=price,
                year=year,
                mileage=mileage,
                url=url,
                location="",
            ))
        except Exception:
            continue

    return listings


async def scrape_facebook(
    page: Page,
    args: argparse.Namespace,
    lat: float,
    lon: float,
) -> list[FBListing]:
    """
    Navigate to FB Marketplace and intercept GraphQL responses to collect listings.
    Falls back to DOM scraping if interception yields nothing.
    """
    url = build_fb_search_url(lat, lon, args)
    console.print(f"[cyan]Searching Facebook Marketplace...[/cyan]")
    console.print(f"[dim]{url}[/dim]")

    collected_responses: list[dict] = []

    async def handle_response(response):
        if "/api/graphql" in response.url and response.status == 200:
            try:
                body = await response.json()
                collected_responses.append(body)
            except Exception:
                pass

    page.on("response", handle_response)

    try:
        await page.goto(url, wait_until="networkidle", timeout=30000)
    except Exception:
        # networkidle can time out on slow connections — proceed anyway
        pass

    # Scroll to lazy-load more listings
    scroll_rounds = math.ceil(args.max_listings / 20)
    for i in range(scroll_rounds):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(random.uniform(*SCROLL_PAUSE_RANGE))
        console.print(f"[dim]Scrolling... ({i+1}/{scroll_rounds})[/dim]", end="\r")

    page.remove_listener("response", handle_response)
    console.print()  # newline after \r progress

    # Parse all intercepted GraphQL responses
    all_listings: list[FBListing] = []
    for body in collected_responses:
        all_listings.extend(parse_fb_response(body))

    if not all_listings:
        console.print("[yellow]No GraphQL listings found — trying DOM fallback.[/yellow]")
        all_listings = await parse_fb_dom_fallback(page, args)

    if not all_listings:
        console.print("[red]No listings found. Try --no-headless to debug.[/red]")
        return []

    filtered = filter_fb_listings(all_listings, args)
    # Cap at max_listings
    filtered = filtered[:args.max_listings]

    console.print(
        f"[green]Found {len(all_listings)} raw listings → "
        f"{len(filtered)} after filtering.[/green]"
    )
    return filtered


# --- AUTOTEMPEST SCRAPER ---

def build_at_url(make: str, model: str, year: int, zip_code: str, radius: int) -> str:
    """
    Build an AutoTempest Price Trends URL for a specific make/model/year.

    Price Trends (/price-trends) differs from /results in that it returns
    both current AND historical listings, yielding a much larger price sample
    and a more reliable market median. It accepts the same GET parameters.
    """
    params = {
        "make": make.lower(),
        "model": model.lower(),
        "zip": zip_code,
        "radius": radius,
        "minyear": year,
        "maxyear": year,
    }
    return AT_PRICE_TRENDS_BASE + "?" + urlencode(params)


def _extract_prices_from_json(data) -> list[int]:
    """
    Recursively search a JSON object (dict or list) for numeric price values.
    AutoTempest's Price Trends chart data is loaded via XHR; when intercepted
    it arrives as a JSON blob with nested price arrays.
    """
    found = []
    if isinstance(data, dict):
        for key, val in data.items():
            # Keys that commonly hold price data in chart/trend APIs
            if key.lower() in ("price", "prices", "amount", "value", "median",
                               "average", "mean", "listing_price", "sale_price"):
                if isinstance(val, (int, float)) and 500 < val < 500000:
                    found.append(int(val))
                elif isinstance(val, list):
                    for v in val:
                        if isinstance(v, (int, float)) and 500 < v < 500000:
                            found.append(int(v))
            else:
                found.extend(_extract_prices_from_json(val))
    elif isinstance(data, list):
        for item in data:
            found.extend(_extract_prices_from_json(item))
    return found


def parse_at_page(html: str) -> list[int]:
    """
    Extract numeric prices from an AutoTempest Price Trends page.

    Strategy (in order of preference):
    1. Displayed aggregate stat — Price Trends shows a median/average price
       prominently near the chart. Try BEM selectors for those summary numbers.
    2. Individual listing cards — li.result-list-item with .badge__label.label--price
       (same markup as /results; Price Trends includes historical listings too).
    3. Broad fallback — any element whose class contains "price".
    """
    soup = BeautifulSoup(html, "lxml")
    prices = []

    # 1. Try to grab the displayed aggregate stat from the Price Trends chart summary.
    #    Common patterns: .price-trend__stat, .trend-summary, .chart-stat, [class*="trend"]
    aggregate_selectors = [
        ".price-trend__stat",
        ".trend-summary__price",
        ".chart-summary__value",
        '[class*="trend"][class*="stat"]',
        '[class*="trend"][class*="price"]',
        '[class*="chart"][class*="price"]',
        '[class*="summary"][class*="price"]',
    ]
    for sel in aggregate_selectors:
        for el in soup.select(sel):
            text = el.get_text(strip=True)
            if re.search(r"\d", text):
                cleaned = re.sub(r"[^\d]", "", text)
                if cleaned:
                    val = int(cleaned)
                    if 500 < val < 500000:
                        prices.append(val)
        if prices:
            break  # stop once we've found the aggregate stat

    # 2. Individual listing cards (current + historical on Price Trends page).
    for item in soup.select("li.result-list-item"):
        try:
            # Primary selector: BEM-named price badge
            price_el = item.select_one(".badge__label.label--price")
            if not price_el:
                # Fallback: any element whose class contains "price"
                price_el = item.select_one('[class*="price"]')
            if not price_el:
                continue

            price_text = price_el.get_text(strip=True)

            # Skip "Call", "Inquire", etc.
            if not re.search(r"\d", price_text):
                continue

            price = int(re.sub(r"[^\d]", "", price_text))
            if price > 500:   # sanity filter — ignore $0 or bogus entries
                prices.append(price)
        except (ValueError, AttributeError):
            continue

    # 3. Broad fallback — sweep for any price-bearing element
    if not prices:
        for el in soup.select('[class*="price"], [data-price]'):
            text = el.get_text(strip=True)
            if not re.search(r"\$\s*[\d,]+", text):
                continue
            cleaned = re.sub(r"[^\d]", "", text)
            if cleaned:
                val = int(cleaned)
                if 500 < val < 500000:
                    prices.append(val)

    return prices


def compute_market_avg(prices: list[int]) -> Optional[float]:
    """Return the median price (robust against dealer-priced outliers)."""
    if not prices:
        return None
    return statistics.median(prices)


async def fetch_at_prices(
    page: Page,
    make: str,
    model: str,
    year: int,
    zip_code: str,
    radius: int,
    cache: dict,
) -> list[int]:
    """
    Fetch price data from AutoTempest's Price Trends page for a make/model/year.

    Price Trends (/price-trends) includes both current and historical listings,
    yielding a larger and more representative price distribution than /results.
    The page renders a JS chart; we intercept any accompanying XHR/fetch calls
    that carry JSON price data (e.g. chart datasets) in addition to scraping the
    rendered listing cards and any displayed aggregate stats.
    """
    cache_key = f"{make.lower()}:{model.lower()}:{year}"
    if cache_key in cache:
        return cache[cache_key]

    url = build_at_url(make, model, year, zip_code, radius)
    console.print(f"[cyan]AutoTempest Price Trends: {make} {model} {year}...[/cyan]")

    prices: list[int] = []
    intercepted_json_prices: list[int] = []

    async def handle_at_response(response):
        """Capture JSON from any XHR/fetch that the Price Trends chart fires."""
        content_type = response.headers.get("content-type", "")
        if "json" in content_type and response.status == 200:
            try:
                body = await response.json()
                intercepted_json_prices.extend(_extract_prices_from_json(body))
            except Exception:
                pass

    for attempt, delay in enumerate([0] + AT_RETRY_DELAYS, start=1):
        if delay:
            await asyncio.sleep(delay)
        intercepted_json_prices.clear()
        page.on("response", handle_at_response)
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if response and response.status == 403:
                page.remove_listener("response", handle_at_response)
                console.print(
                    f"[yellow]AutoTempest 403 (attempt {attempt}) — backing off.[/yellow]"
                )
                if attempt > len(AT_RETRY_DELAYS):
                    break
                continue

            # Wait for the Price Trends chart or listing results to appear.
            # The chart (canvas/svg) renders after the initial XHR completes.
            try:
                await page.wait_for_selector(
                    "canvas, svg.chart, .price-trend, "
                    ".result-list-item, .no-results",
                    timeout=20000,
                )
            except Exception:
                pass

            # Let the chart finish rendering and any deferred XHR calls settle
            await asyncio.sleep(random.uniform(*REQUEST_DELAY_RANGE))

            page.remove_listener("response", handle_at_response)

            # Prefer XHR-intercepted prices (direct data source, no DOM parsing needed)
            if intercepted_json_prices:
                prices = intercepted_json_prices[:]
                console.print(
                    f"[dim]  → {len(prices)} prices from chart API[/dim]"
                )
            else:
                # Fall back to parsing the rendered HTML
                html = await page.content()
                prices = parse_at_page(html)

            break
        except Exception as e:
            page.remove_listener("response", handle_at_response)
            console.print(f"[yellow]AT fetch error (attempt {attempt}): {e}[/yellow]")
            if attempt > len(AT_RETRY_DELAYS):
                break

    if prices:
        console.print(
            f"[dim]  → {len(prices)} prices found, median ${statistics.median(prices):,.0f}[/dim]"
        )
    else:
        console.print(f"[yellow]  → No prices found for {make} {model} {year}[/yellow]")

    cache[cache_key] = prices
    return prices


# --- DEAL ANALYSIS ---

async def analyze_listings(
    fb_listings: list[FBListing],
    cache: dict,
    at_page: Page,
    args: argparse.Namespace,
) -> list[DealResult]:
    """Cross-reference FB listings against AutoTempest and compute deal scores."""
    results: list[DealResult] = []

    # Collect unique years to minimize AT fetches
    years_needed = {l.year for l in fb_listings if l.year is not None}

    # Pre-fetch AT prices for each unique year
    at_prices_by_year: dict[int, list[int]] = {}
    for year in sorted(years_needed):
        prices = await fetch_at_prices(
            at_page,
            args.make, args.model, year,
            args.zip, args.radius,
            cache,
        )
        at_prices_by_year[year] = prices

    for listing in fb_listings:
        if listing.year is not None:
            raw_prices = at_prices_by_year.get(listing.year, [])
        else:
            raw_prices = []

        market_avg = compute_market_avg(raw_prices)

        if market_avg and listing.price:
            discount_pct = ((listing.price - market_avg) / market_avg) * 100
            is_deal = discount_pct <= -args.min_discount
        else:
            discount_pct = None
            is_deal = False

        results.append(DealResult(
            listing=listing,
            market_avg=market_avg,
            at_sample_count=len(raw_prices),
            discount_pct=discount_pct,
            is_deal=is_deal,
        ))

    # Sort: deals first, then by discount_pct ascending (biggest discount on top)
    results.sort(key=lambda r: (
        not r.is_deal,
        r.discount_pct if r.discount_pct is not None else 0.0,
    ))
    return results


# --- OUTPUT ---

def render_rich_table(results: list[DealResult], args: argparse.Namespace) -> None:
    table = Table(
        title=f"Facebook Marketplace — {args.make.title()} {args.model.title()} Deals "
              f"(ZIP {args.zip}, {args.radius} mi, last {args.days_listed} days)",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold white on dark_blue",
        row_styles=["", "dim"],
        expand=True,
    )

    table.add_column("#",         style="dim",           width=3,  justify="right")
    table.add_column("Year",      style="cyan",          width=6,  justify="center")
    table.add_column("Title",     style="white",         min_width=22, no_wrap=False)
    table.add_column("Mileage",   style="yellow",        width=10, justify="right")
    table.add_column("Ask Price", style="bold green",    width=10, justify="right")
    table.add_column("Mkt Median",style="blue",          width=11, justify="right")
    table.add_column("Discount",  style="bold",          width=10, justify="right")
    table.add_column("Deal?",     style="bold",          width=7,  justify="center")
    table.add_column("Location",  style="dim",           width=16, no_wrap=True)
    table.add_column("URL",       style="dim underline", min_width=18, no_wrap=True)

    for i, r in enumerate(results, 1):
        if r.discount_pct is None:
            disc_str = "[dim]N/A[/dim]"
        elif r.discount_pct <= -args.min_discount:
            disc_str = f"[bold green]{r.discount_pct:+.1f}%[/bold green]"
        elif r.discount_pct < 0:
            disc_str = f"[green]{r.discount_pct:+.1f}%[/green]"
        else:
            disc_str = f"[red]{r.discount_pct:+.1f}%[/red]"

        deal_str = "[bold green]YES[/bold green]" if r.is_deal else "[dim]no[/dim]"
        mkt_str  = f"${r.market_avg:,.0f}" if r.market_avg else "[dim]N/A[/dim]"
        ask_str  = f"${r.listing.price:,}" if r.listing.price else "[dim]N/A[/dim]"
        mile_str = f"{r.listing.mileage:,}" if r.listing.mileage else "[dim]N/A[/dim]"

        table.add_row(
            str(i),
            str(r.listing.year or "N/A"),
            r.listing.title,
            mile_str,
            ask_str,
            mkt_str,
            disc_str,
            deal_str,
            r.listing.location or "",
            r.listing.url,
        )

    console.print(table)
    deal_count = sum(1 for r in results if r.is_deal)
    console.print(
        f"\n[bold]Deals flagged (≥{args.min_discount:.0f}% below market): "
        f"{deal_count} / {len(results)}[/bold]"
    )


def write_csv(
    results: list[DealResult],
    output_path: str,
    cache: dict,
    args: argparse.Namespace,
) -> None:
    fieldnames = [
        "rank", "listing_id", "title", "year", "asking_price",
        "mileage", "location", "market_median_price", "discount_pct",
        "at_sample_count", "is_deal", "fb_url", "scraped_at",
    ]
    scraped_at = datetime.datetime.now().isoformat(timespec="seconds")

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for i, r in enumerate(results, 1):
            writer.writerow({
                "rank":                i,
                "listing_id":          r.listing.id,
                "title":               r.listing.title,
                "year":                r.listing.year or "",
                "asking_price":        r.listing.price or "",
                "mileage":             r.listing.mileage or "",
                "location":            r.listing.location,
                "market_median_price": round(r.market_avg, 2) if r.market_avg else "",
                "discount_pct":        round(r.discount_pct, 2) if r.discount_pct is not None else "",
                "at_sample_count":     r.at_sample_count,
                "is_deal":             r.is_deal,
                "fb_url":              r.listing.url,
                "scraped_at":          scraped_at,
            })

    console.print(f"[green]Results saved to {output_path}[/green]")


# --- MAIN ---

async def main() -> None:
    args = parse_args()

    # Resolve output path
    if args.output is None:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        args.output = f"car_deals_{ts}.csv"

    console.print(
        f"\n[bold cyan]Car Deal Finder[/bold cyan] — "
        f"{args.make.title()} {args.model.title()} "
        f"{args.min_year}–{args.max_year}, "
        f"max ${args.max_price:,}, "
        f"ZIP {args.zip} ({args.radius} mi), "
        f"last {args.days_listed} days\n"
    )

    cache = load_cache(args.cache_file)

    email, password = get_fb_credentials(args)

    # Geocode ZIP
    console.print(f"[dim]Geocoding ZIP {args.zip}...[/dim]")
    try:
        lat, lon = zip_to_latlon(args.zip)
        console.print(f"[dim]  → lat={lat:.4f}, lon={lon:.4f}[/dim]")
    except ValueError as e:
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)

    async with async_playwright() as playwright:
        browser, context = await build_browser_context(playwright, args)

        # Apply stealth to all pages
        fb_page = await context.new_page()
        await _apply_stealth(fb_page)
        at_page = await context.new_page()
        await _apply_stealth(at_page)

        try:
            # --- Facebook login & scraping ---
            needs_login = not pathlib.Path(args.session_file).exists()
            if not needs_login:
                # Quick check: navigate to FB and see if we're still logged in
                await fb_page.goto("https://www.facebook.com", wait_until="domcontentloaded", timeout=15000)
                if "login" in fb_page.url:
                    needs_login = True

            if needs_login:
                await fb_login(fb_page, email, password)
                await save_session(context, args.session_file)

            fb_listings = await scrape_facebook(fb_page, args, lat, lon)

            if not fb_listings:
                console.print(
                    "[yellow]No Facebook listings to process. "
                    "Try adjusting your search parameters.[/yellow]"
                )
                return

            # --- AutoTempest cross-reference ---
            console.print(
                f"\n[cyan]Cross-referencing {len(fb_listings)} listing(s) "
                f"against AutoTempest...[/cyan]\n"
            )
            results = await analyze_listings(fb_listings, cache, at_page, args)

            # --- Output ---
            console.print()
            render_rich_table(results, args)
            write_csv(results, args.output, cache, args)

        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/yellow]")
        finally:
            save_cache(args.cache_file, cache)
            await context.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
