"""
Contact scraper — finds a brand's website from Target's brand-page slug,
then scrapes it for emails and phone numbers.

Website lookup strategy
-----------------------
Target brand hrefs look like:  /b/hanes-premium/-/N-55f3l
                                     ↑ slug
We strip trailing company suffixes (inc, llc, …) then try URL candidates
in parallel using fast HEAD requests — no external search engines needed.

  slug  : hanes-premium
  tries : www.hanesbrands.com? → no   (guessing removed)
          www.hanespremium.com  → HEAD → ✓  use it
          www.hanes-premium.com → HEAD → …
"""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup


# ── Constants ────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

CONTACT_PATHS = ["/contact-us", "/contact", "/about", "/support"]

IGNORED_DOMAINS = {
    "sentry.io", "example.com", "target.com", "w3.org", "schema.org",
    "google.com", "facebook.com", "wixpress.com", "shopify.com",
    "amazonaws.com", "cloudflare.com", "apple.com", "microsoft.com",
    "jquery.com", "gstatic.com", "googleapis.com",
}

EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", re.I
)
PHONE_RE = re.compile(
    r"(\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}"
)

# Company-type suffixes to strip before building URL candidates
_SLUG_SUFFIXES = re.compile(
    r"-(inc|llc|corp|co|ltd|company|group|brands|"
    r"international|enterprises|studios?)$",
    re.I,
)


# ── Slug / URL helpers ───────────────────────────────────────────────────────

def _slug_from_href(brand_href: str) -> str:
    """
    '/b/hanes-premium/-/N-55f3l'  →  'hanes-premium'
    """
    parts = brand_href.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "b":
        return parts[1]
    return ""


def find_website_from_slug(brand_href: str) -> str | None:
    """
    Derive URL candidates from Target's brand-page slug and return the first
    one that resolves (verified with parallel HEAD requests).

    /b/hanes-premium/-/N-55f3l
      slug  → hanes-premium
      strip → hanes-premium   (no suffix to remove)
      tries → www.hanespremium.com, www.hanes-premium.com, …
    """
    slug = _slug_from_href(brand_href)
    if not slug:
        return None

    clean       = _SLUG_SUFFIXES.sub("", slug).strip("-")
    no_hyphens  = clean.replace("-", "")   # hanespremium
    with_hyphens = clean                   # hanes-premium

    # Preserve insertion order, remove dupes
    candidates = list(dict.fromkeys([
        f"https://www.{no_hyphens}.com",
        f"https://www.{with_hyphens}.com",
        f"https://{no_hyphens}.com",
        f"https://{with_hyphens}.com",
    ]))

    def _check(url: str) -> str | None:
        resp = _head(url)
        return resp.url if resp else None

    # Don't use `with` — its __exit__ calls shutdown(wait=True) which blocks
    # until every remaining thread finishes even when we return early.
    pool = ThreadPoolExecutor(max_workers=len(candidates))
    futures = {pool.submit(_check, u): u for u in candidates}
    found = None
    for future in as_completed(futures):
        result = future.result()
        if result:
            found = result
            break
    pool.shutdown(wait=False)   # abandon any still-running HEAD requests
    return found


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _head(url: str, timeout: int = 3) -> requests.Response | None:
    try:
        r = requests.head(
            url, headers=HEADERS, timeout=timeout, allow_redirects=True
        )
        return r if r.status_code < 400 else None
    except Exception:
        return None


def _get(url: str, timeout: int = 5) -> requests.Response | None:
    try:
        r = requests.get(
            url, headers=HEADERS, timeout=timeout, allow_redirects=True
        )
        r.raise_for_status()
        return r
    except Exception:
        return None


# ── Extraction ───────────────────────────────────────────────────────────────

def _extract(html: str) -> tuple[list[str], list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "meta"]):
        tag.decompose()

    emails: list[str] = []

    # mailto: links
    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        if href.startswith("mailto:"):
            addr = href[7:].split("?")[0].strip()
            if "@" in addr and not _bad_domain(addr.split("@")[-1]):
                emails.append(addr)

    # plain text scan
    for m in EMAIL_RE.findall(soup.get_text(separator=" ")):
        domain = m.split("@")[-1].lower()
        if not _bad_domain(domain):
            emails.append(m.lower())

    phones: list[str] = []

    # tel: links
    for a in soup.find_all("a", href=True):
        if a["href"].lower().startswith("tel:"):
            num = a["href"][4:].strip()
            if len(re.sub(r"\D", "", num)) >= 7:
                phones.append(num)

    # plain text scan
    for m in PHONE_RE.finditer(soup.get_text(separator=" ")):
        full = m.group(0).strip()
        if len(re.sub(r"\D", "", full)) >= 10:
            phones.append(full)

    return _dedupe(emails), _dedupe(phones)


def _bad_domain(domain: str) -> bool:
    return any(bad in domain for bad in IGNORED_DOMAINS)


def _fetch_and_extract(url: str) -> tuple[list[str], list[str]]:
    resp = _get(url)
    return _extract(resp.text) if resp else ([], [])


def _dedupe(lst: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in lst:
        k = x.lower().strip()
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


# ── Public API ───────────────────────────────────────────────────────────────

def get_contact_info(website: str = "", brand_href: str = "") -> dict:
    """
    1. Resolve the website URL:
       - Use `website` directly if it was provided (from Target's JSON-LD)
       - Otherwise derive from the `brand_href` slug
       - Return empty immediately if neither resolves
    2. Scrape homepage + contact pages in parallel for emails / phones.
    """
    result: dict = {"website": None, "emails": [], "phones": []}

    resolved = website.strip()
    if not resolved and brand_href:
        resolved = find_website_from_slug(brand_href) or ""

    if not resolved:
        return result

    parsed = urlparse(resolved)
    base   = f"{parsed.scheme}://{parsed.netloc}"
    result["website"] = base

    urls = [base] + [urljoin(base, p) for p in CONTACT_PATHS]

    all_emails: list[str] = []
    all_phones: list[str] = []

    # Same pattern: no `with` so we can shut down without blocking
    pool    = ThreadPoolExecutor(max_workers=len(urls))
    futures = [pool.submit(_fetch_and_extract, u) for u in urls]
    for future in as_completed(futures):
        emails, phones = future.result()
        all_emails.extend(emails)
        all_phones.extend(phones)
    pool.shutdown(wait=False)

    result["emails"] = _dedupe(all_emails)
    result["phones"] = _dedupe(all_phones)
    return result
