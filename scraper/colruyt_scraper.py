"""
Colruyt products scraper - Python-versie.

Gebaseerd op https://github.com/BelgianNoise/colruyt-products-scraper (origineel in Go).

Werking:
  1. Haal sessie-cookies op via een headless browser (Playwright).
  2. Haal de X-CG-APIKey op uit een publiek JSON-bestand.
  3. Doe een init-call om het totaal aantal producten te kennen.
  4. Loop over alle pagina's en verzamel + dedupliceer de producten.
  5. Schrijf alles weg naar een JSON-bestand.

Installatie:
    pip install requests playwright
    playwright install chromium

Gebruik:
    python colruyt_scraper.py --place-id 604 --out producten.json

Vind je place-id (winkel) in de network-tab op colruyt.be, of probeer een
collect-punt / winkel-ID. Zonder geldige placeId krijg je geen prijzen.
"""

import argparse
import json
import re
import sys
import time
import os

import requests
from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Constanten (overgenomen uit het Go-project)
# ---------------------------------------------------------------------------
PRODUCTS_ENDPOINT = (
    "https://apip.colruyt.be/gateway/"
    "ictmgmt.emarkecom.cgproductretrsvc.v2/v2/v2/nl/products"
)
MODEL_JSON_URL = "https://www.colruyt.be/content/clp/nl.model.json"
PRODUCTS_PAGE_URL = "https://www.colruyt.be/nl/producten"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36 OPR/107.0.0.0"
)

# De auteur zegt dat deze key bijna nooit verandert. Wordt automatisch
# opgehaald, maar je kan hem als fallback hardcoden.
FALLBACK_API_KEY = ""


# ---------------------------------------------------------------------------
# Stap 1: cookies ophalen via een headless browser
# ---------------------------------------------------------------------------
def get_session_cookies(headless: bool = True) -> dict:
    """Start een headless browser, surf naar colruyt.be en geef de cookies terug.

    Dit is de anti-bot-laag: de site zet cookies die de API verwacht.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright ontbreekt. Run: pip install playwright && "
              "playwright install chromium", file=sys.stderr)
        raise

    cookies = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()
        page.goto(PRODUCTS_PAGE_URL, wait_until="domcontentloaded")
        print("Wachten (10s) tot de sessie geladen is ...")
        time.sleep(10)
        for c in context.cookies():
            cookies[c["name"]] = c["value"]
        browser.close()
    print(f"{len(cookies)} cookies opgehaald.")
    return cookies


# ---------------------------------------------------------------------------
# Stap 2: de X-CG-APIKey ophalen
# ---------------------------------------------------------------------------
def get_api_key() -> str:
    """Haal de X-CG-APIKey uit het publieke model.json-bestand."""
    if FALLBACK_API_KEY:
        return FALLBACK_API_KEY
    resp = requests.get(MODEL_JSON_URL, headers={"User-Agent": USER_AGENT}, timeout=15)
    resp.raise_for_status()
    match = re.search(r'"X-CG-APIKey: ([a-zA-Z0-9-]+)"', resp.text)
    if not match:
        raise RuntimeError("X-CG-APIKey niet gevonden in model.json")
    return match.group(1)


# ---------------------------------------------------------------------------
# Stap 3 & 4: API-calls
# ---------------------------------------------------------------------------
def do_api_call(session: requests.Session, api_key: str,page: int, size: int) -> dict:
    """Eén call naar de product-API."""
    params = {
        "clientCode": "CLP",
        "page": page,
        "size": size,
        "placeId": os.getenv("placeID"),
        "sort": "basicprice asc",
    }
    headers = {"User-Agent": USER_AGENT, "X-Cg-Apikey": api_key}
    resp = session.get(PRODUCTS_ENDPOINT, params=params, headers=headers, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"Status {resp.status_code} op pagina {page}: {resp.text[:200]}")
    return resp.json()


def scrape_all_products(page_size: int = 250,
                        delay: float = 1.5, headless: bool = True) -> list:
    """Volledige scrape-flow. Geeft een lijst van product-dicts terug."""
    cookies = get_session_cookies(headless=headless)
    api_key = get_api_key()
    print(f"API-key: {api_key}")

    session = requests.Session()
    session.cookies.update(cookies)

    # Init-call om het totaal te kennen
    init = do_api_call(session, api_key, page=1, size=1)
    total = init.get("productsFound", 0)
    pages = total // page_size + 1
    print(f"{total} producten gevonden -> {pages} pagina's van {page_size}")

    products = []
    seen = set()
    for page in range(1, pages + 1):
        try:
            data = do_api_call(session, api_key, page, page_size)
        except RuntimeError as e:
            print(f"  Fout (overslaan): {e}", file=sys.stderr)
            # Bij rate limiting (vaak status 456) zou je hier de cookies
            # opnieuw moeten ophalen. Voor de eenvoud slaan we de pagina over.
            time.sleep(delay * 3)
            continue

        new = 0
        for prod in data.get("products", []):
            pid = prod.get("productId")
            if pid and pid not in seen:
                seen.add(pid)
                products.append(prod)
                new += 1
        print(f"  pagina {page}/{pages}: +{new} (totaal {len(products)})")
        time.sleep(delay)  # vriendelijk blijven -> minder kans op ban

    return products


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Colruyt products scraper (Python)")
    parser.add_argument("--out", default="colruyt_producten.json", help="Output JSON-bestand")
    parser.add_argument("--page-size", type=int, default=250)
    parser.add_argument("--delay", type=float, default=1.5, help="Seconden tussen calls")
    parser.add_argument("--no-headless", action="store_true", help="Browser zichtbaar tonen")
    args = parser.parse_args()

    products = scrape_all_products(
        page_size=args.page_size,
        delay=args.delay,
        headless=not args.no_headless,
    )

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)
    print(f"Klaar: {len(products)} producten weggeschreven naar {args.out}")


if __name__ == "__main__":
    main()
