"""
Target.com Sponsored Ad Scraper

Usage:
    python scraper.py --term "shirts"
    python scraper.py --term "running shoes" --output results.csv
    python scraper.py --term "laptops" --output results.json
"""

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, asdict

import requests as _requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout


BASE_URL   = "https://www.target.com/s?searchTerm={term}"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
PHONE_RE  = re.compile(r"\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}")

# Email noise to ignore when scraping brand sites
_EMAIL_SKIP = ("noreply", "no-reply", "donotreply", "example.", "sentry.",
               "shopify.", "wix.", "google.", "yourname@", "@email.")

_REQ_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


@dataclass
class SponsoredAd:
    title:       str
    brand:       str
    price:       str
    product_url: str
    image_url:   str
    search_term: str
    brand_href:  str = ""   # e.g. /b/hanes-premium/-/N-55f3l
    website:     str = ""   # domain extracted from seller email


# ── Fallback: guess brand website by trying common domain patterns ────────────

def _search_brand_website(brand: str, browser=None) -> str:
    """
    Build candidate domains from the brand name and check each one with a
    HEAD request. Returns the first domain that responds with a non-error
    status, or '' if none resolve.

    Examples:
        'Milk-Bone'  -> milkbone.com
        'Zesty Paws' -> zestypaws.com
        'Garmin'     -> garmin.com
    """
    if not brand:
        return ""

    clean = re.sub(r"[^a-z0-9]", "", brand.lower())
    words = re.sub(r"[^a-z0-9 ]", "", brand.lower()).split()

    candidates: list[str] = []
    if clean:
        candidates += [
            f"{clean}.com",
            f"the{clean}.com",
            f"{clean}brand.com",
        ]
    # Also try hyphenated version for multi-word brands (e.g. pet-honesty.com)
    if len(words) > 1:
        hyphenated = "-".join(words)
        candidates.append(f"{hyphenated}.com")

    for domain in candidates:
        try:
            r = _requests.head(
                f"https://{domain}",
                headers=_REQ_HEADERS,
                timeout=5,
                allow_redirects=True,
            )
            if r.status_code < 400:
                return domain
        except Exception:
            continue

    return ""


# ── Brand website enrichment ──────────────────────────────────────────────────

def _get_brand_website(product_url: str, browser) -> str:
    """
    1. Visit the product page and grab the /sp/ seller-store URL from the
       'Sold & shipped by' link.
    2. Visit the /sp/ page and JS-click the Privacy or Contact tab.
    3. Regex-find the first email address, return just the domain.
       e.g.  support@thejunipershopwholesale.com  ->  thejunipershopwholesale.com
    """
    if not product_url:
        return ""

    try:
        page = browser.new_page(user_agent=USER_AGENT)

        # ── Step 1: product page → find /sp/ URL ─────────────────────────────
        page.goto(product_url, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        page.keyboard.press("Escape")   # dismiss any location/promo overlay
        page.wait_for_timeout(300)

        sp_url = page.evaluate(
            "() => document.querySelector('a[href*=\"/sp/\"]')?.href || null"
        )

        if not sp_url:
            page.close()
            return ""

        # ── Step 2: seller page → click Privacy or Contact tab ───────────────
        page.goto(sp_url, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        page.evaluate("""() => {
            for (const t of document.querySelectorAll('[role=tab]')) {
                const txt = t.textContent.trim();
                if (txt === 'Privacy' || txt === 'Contact') { t.click(); break; }
            }
        }""")
        page.wait_for_timeout(1500)

        text = page.inner_text("body")
        page.close()

        match = EMAIL_RE.search(text)
        if match:
            return match.group(0).split("@")[1].lower()

    except Exception:
        pass

    return ""


# ── Contact enrichment ───────────────────────────────────────────────────────

_CONTACT_PATHS = [
    "",             # homepage
    "/contact",
    "/contact-us",
    "/pages/contact",
    "/pages/contact-us",
    "/about",
    "/about-us",
]


def _scrape_contacts_with_browser(domain: str, browser) -> dict:
    """
    Visit the brand's own website and pull back any emails and phone numbers.
    Tries the homepage plus common contact/about paths.
    """
    base    = f"https://{domain}"
    emails: set[str] = set()
    phones: set[str] = set()

    try:
        page = browser.new_page(user_agent=USER_AGENT)

        for path in _CONTACT_PATHS:
            url = base + path
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=8000)
                if not resp or resp.status >= 400:
                    continue
                page.wait_for_timeout(800)
                text = page.inner_text("body")

                for e in EMAIL_RE.findall(text):
                    if not any(skip in e.lower() for skip in _EMAIL_SKIP):
                        emails.add(e.lower())

                for ph in PHONE_RE.findall(text):
                    phones.add(ph.strip())

            except Exception:
                continue

        page.close()

    except Exception:
        pass

    # Keep only phone strings that contain at least 10 digits
    clean_phones = sorted(
        {ph for ph in phones if len(re.sub(r"\D", "", ph)) >= 10}
    )
    return {"emails": sorted(emails), "phones": clean_phones}


def scrape_contacts(domain: str) -> dict:
    """Public entry-point: launch a fresh browser and scrape contact info."""
    if not domain:
        return {"emails": [], "phones": []}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        result  = _scrape_contacts_with_browser(domain, browser)
        browser.close()
    return result


# ── Main scrape ───────────────────────────────────────────────────────────────

def scrape(search_term: str) -> list[SponsoredAd]:
    url = BASE_URL.format(term=search_term.replace(" ", "+"))
    print(f"Loading: {url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        # ── Step 1: search results page ──────────────────────────────────────
        page = browser.new_page(user_agent=USER_AGENT)
        page.goto(url, wait_until="domcontentloaded")

        try:
            page.wait_for_selector(
                '[data-test="@web/site-top-of-funnel/ProductCardWrapper"]',
                timeout=15_000,
            )
        except PlaywrightTimeout:
            print("Timed out waiting for product cards.")
            browser.close()
            return []

        page.wait_for_timeout(2000)
        html = page.content()
        page.close()

        # ── Step 2: parse sponsored cards ────────────────────────────────────
        soup  = BeautifulSoup(html, "html.parser")
        cards = soup.find_all(
            attrs={"data-test": "@web/site-top-of-funnel/ProductCardWrapper"}
        )
        print(f"Found {len(cards)} product cards -- scanning for sponsored...")

        ads: list[SponsoredAd] = []
        for card in cards:
            ad = _parse_card(card, search_term)
            if ad:
                ads.append(ad)

        print(f"  -> {len(ads)} sponsored ad(s) found")

        # ── Step 3: enrich each unique brand with a website ──────────────────
        if ads:
            print("Fetching brand websites...")
            # Cache by brand_href so duplicate brands only get scraped once
            cache: dict[str, str] = {}   # brand_href -> domain
            for ad in ads:
                if not ad.brand_href:
                    continue
                if ad.brand_href in cache:
                    ad.website = cache[ad.brand_href]
                    continue
                print(f"  Checking {ad.brand}...")
                domain = _get_brand_website(ad.product_url, browser)
                cache[ad.brand_href] = domain
                ad.website = domain
                if domain:
                    print(f"    -> {domain}")

        # ── Step 4: fallback search for brands still missing a website ────────
        if ads:
            missing = [ad for ad in ads if not ad.website and ad.brand]
            if missing:
                print(f"Searching web for {len(missing)} brand(s) with no website...")
                search_cache: dict[str, str] = {}  # brand -> domain
                for ad in missing:
                    if ad.brand in search_cache:
                        ad.website = search_cache[ad.brand]
                        continue
                    print(f"  Searching for {ad.brand}...")
                    domain = _search_brand_website(ad.brand, browser)
                    search_cache[ad.brand] = domain
                    ad.website = domain
                    if domain:
                        print(f"    -> {domain}")

        browser.close()

    return ads


# ── Card parser ───────────────────────────────────────────────────────────────

def _parse_card(card, search_term: str) -> SponsoredAd | None:
    # Only process cards that have the "Sponsored" label
    if not card.find(attrs={"data-test": "sponsoredText"}):
        return None

    # Title + product URL
    title_el = card.find(attrs={"data-test": "@web/ProductCard/title"})
    title    = title_el.get_text(strip=True) if title_el else ""
    href     = title_el.get("href", "") if title_el else ""
    url      = f"https://www.target.com{href}" if href.startswith("/") else href

    # Brand + brand page href
    brand_el   = card.find(
        attrs={"data-test": "@web/ProductCard/ProductCardBrandAndRibbonMessage/brand"}
    )
    brand      = brand_el.get_text(strip=True) if brand_el else ""
    brand_href = brand_el.get("href", "")      if brand_el else ""

    # Price
    price_el = card.find(attrs={"data-test": "current-price"})
    price    = price_el.get_text(strip=True) if price_el else ""

    # Primary product image
    img_wrapper = card.find(
        "picture",
        attrs={"data-test": "@web/ProductCard/ProductCardImage/primary"},
    )
    img_el    = img_wrapper.find("img") if img_wrapper else None
    image_url = img_el.get("src", "") if img_el else ""

    return SponsoredAd(
        title=title,
        brand=brand,
        price=price,
        product_url=url,
        image_url=image_url,
        search_term=search_term,
        brand_href=brand_href,
    )


# ── Output ────────────────────────────────────────────────────────────────────

def save_csv(ads: list[SponsoredAd], path: str) -> None:
    keys = list(asdict(ads[0]).keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(asdict(ad) for ad in ads)
    print(f"Saved -> {path}")


def save_json(ads: list[SponsoredAd], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(ad) for ad in ads], f, indent=2, ensure_ascii=False)
    print(f"Saved -> {path}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape sponsored ads from Target.com search results."
    )
    parser.add_argument("--term",   required=True, help='Search term, e.g. "shirts"')
    parser.add_argument("--output", default=None,  help="Output file (.csv or .json)")
    args = parser.parse_args()

    ads = scrape(args.term)

    if not ads:
        print("No sponsored ads found.")
        sys.exit(0)

    print(f"\n{'-'*60}")
    for i, ad in enumerate(ads, 1):
        print(f"[{i}] {ad.title}")
        print(f"     Brand   : {ad.brand}")
        print(f"     Price   : {ad.price}")
        print(f"     Website : {ad.website or '(not found)'}")
        print(f"     URL     : {ad.product_url}")
        print()

    if args.output:
        if args.output.endswith(".json"):
            save_json(ads, args.output)
        else:
            save_csv(ads, args.output)


if __name__ == "__main__":
    main()
