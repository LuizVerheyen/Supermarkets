"""
Delhaize.be product scraper - v3

Gebruikt de interne GraphQL API (Apollo persisted queries).

INSTALLATIE:
    pip install "httpx[http2]"

GEBRUIK:
    # Eerste keer: test met 5 producten om te checken of het werkt
    python delhaize_scraper.py --test --limit 5

    # Test 1 categorie (~194 producten Sport & Gezondheid)
    python delhaize_scraper.py --test

    # Specifieke categoriecode (zie ze in URL: .../c/CODE)
    python delhaize_scraper.py --category v2SPO

    # Alle bekende hoofdcategorieen
    python delhaize_scraper.py --all

    # Debug: print eerste product JSON volledig (om te zien welke velden er zijn)
    python delhaize_scraper.py --test --limit 1 --debug

ALS JE 403 FOUTEN KRIJGT:
    Delhaize gebruikt Akamai bot protection. Workaround:
    1. Open Firefox, ga naar delhaize.be
    2. F12 > Storage > Cookies > delhaize.be
    3. Kopieer de Cookie HEADER string (zie cURL "Cookie: ..." regel)
    4. Plak in cookies.txt naast dit script
    5. Run met --cookies cookies.txt
"""

import argparse
import httpx
import json
import time
import random
import sqlite3
import sys
import os
from datetime import datetime

# Forceer UTF-8 op Windows console (anders zie je 'GerlinÚa' i.p.v. 'Gerlinéa')
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_URL = "https://www.delhaize.be/api/v1/"
HOME_URL = "https://www.delhaize.be/nl/shop"

QUERY_HASHES = {
    "GetCategoryProductSearch": "189e7cb5a6ba93e55dc63e4eef0ad063ca3e8aedb0bdf2a58124e02d5d5d69a2",
    "ProductDetails": "609c30f5424b88f416ce83bd24aae7921ba670e391a2941267f171d1741047ab",
}

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:151.0) Gecko/20100101 Firefox/151.0",
    "Accept": "*/*",
    "Accept-Language": "nl,en-US;q=0.9,en;q=0.8",
    # Bewust geen 'br' of 'zstd' — vereist extra pakketten en httpx kan gzip/deflate
    # native. Als je 'br' wilt, installeer 'brotli' en zet 'br' terug.
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.delhaize.be/nl/shop",
    "Origin": "https://www.delhaize.be",
    "content-type": "application/json",
    "apollographql-client-name": "be-dll-web-stores",
    "apollographql-client-version": "f50cdf8806badc5789d6fff5a3528c430572bcee",
    "x-default-gql-refresh-token-disabled": "true",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Connection": "keep-alive",
}

DELAY_MIN = 0.3
DELAY_MAX = 0.8
DB_PATH = "delhaize.db"


def parse_cookie_string(cookie_str: str) -> dict:
    """Parse 'a=1; b=2; c=3' naar dict."""
    out = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def make_client(cookie_file=None) -> httpx.Client:
    client = httpx.Client(
        headers=DEFAULT_HEADERS,
        http2=True,
        timeout=30.0,
        follow_redirects=True,
    )
    # Optie 1: gebruik meegegeven cookies
    if cookie_file and os.path.exists(cookie_file):
        with open(cookie_file) as f:
            cookies = parse_cookie_string(f.read().strip())
        print(f"-> Geladen {len(cookies)} cookies uit {cookie_file}")
        for k, v in cookies.items():
            client.cookies.set(k, v, domain=".delhaize.be")
        return client

    # Optie 2: probeer cookies te krijgen via homepage
    print("-> Bezoek homepage om verse cookies te krijgen...")
    try:
        r = client.get(HOME_URL)
        print(f"   Status: {r.status_code} | {len(client.cookies)} cookies")
        if r.status_code == 403:
            print("   ! HTTP 403 op homepage — Akamai bot protection.")
            print("   ! Gebruik --cookies cookies.txt met cookies uit je browser.")
            print("   ! Zie instructies bovenaan dit script.")
    except Exception as e:
        print(f"   ! Fout: {e}")
    return client


def graphql_get(client, operation_name, variables, referer=None, retries=3, debug=False):
    sha = QUERY_HASHES[operation_name]
    params = {
        "operationName": operation_name,
        "variables": json.dumps(variables, separators=(",", ":")),
        "extensions": json.dumps(
            {"persistedQuery": {"version": 1, "sha256Hash": sha}},
            separators=(",", ":"),
        ),
    }
    headers = {
        "x-apollo-operation-name": operation_name,
        "x-apollo-operation-id": sha,
    }
    if referer:
        headers["Referer"] = referer

    last_err = None
    for attempt in range(retries):
        try:
            r = client.get(BASE_URL, params=params, headers=headers)
            if debug:
                print(f"   [debug] {r.status_code} {r.url}")
            if r.status_code == 200:
                data = r.json()
                if "errors" in data:
                    raise RuntimeError(f"GraphQL errors: {data['errors']}")
                return data["data"]
            elif r.status_code in (403, 429):
                wait = 10 * (attempt + 1) + random.uniform(0, 5)
                print(f"     ! HTTP {r.status_code}, wacht {wait:.1f}s...")
                time.sleep(wait)
                last_err = f"HTTP {r.status_code}"
            else:
                r.raise_for_status()
        except httpx.HTTPError as e:
            last_err = str(e)
            time.sleep(2 ** attempt + random.uniform(0, 2))
    raise RuntimeError(f"Faalde na {retries} pogingen: {last_err}")


def fetch_category_page(client, category_code, page, debug=False):
    variables = {
        "lang": "nl",
        "searchQuery": ":relevance",
        "sort": "relevance",
        "category": category_code,
        "pageNumber": page,
        "pageSize": 20,
        "filterFlag": True,
        "fields": "PRODUCT_TILE",
        "plainChildCategories": True,
    }
    referer = f"https://www.delhaize.be/nl/shop/c/{category_code}?q=:relevance&sort=relevance"
    return graphql_get(client, "GetCategoryProductSearch", variables,
                       referer=referer, debug=debug)


def fetch_product_details(client, product_code, product_url=None, debug=False):
    """Haal voedingswaarden, ingredienten en Nutri-Score op voor 1 product."""
    variables = {"productCode": product_code, "lang": "nl"}
    referer = product_url or f"https://www.delhaize.be/nl/shop/p/{product_code}"
    return graphql_get(client, "ProductDetails", variables,
                       referer=referer, debug=debug)


def scrape_category(client, category_code, category_name="", debug=False):
    page = 0
    while True:
        try:
            data = fetch_category_page(client, category_code, page, debug=debug)
        except Exception as e:
            print(f"  X Fout bij {category_code} p{page}: {e}")
            break

        result = data.get("categoryProductSearch") or {}
        products = result.get("products", []) or []
        pagination = result.get("pagination") or {}
        total_pages = pagination.get("totalPages", 1)
        total_results = pagination.get("totalResults", 0)

        if page == 0:
            print(f"  -> {category_name or category_code}: "
                  f"{total_results} producten / {total_pages} pagina's")
            if debug and products:
                print("\n   [DEBUG] Eerste product JSON (volledig):")
                print(json.dumps(products[0], indent=2, ensure_ascii=False))
                print("\n   [DEBUG] Beschikbare top-level velden:")
                print("   " + ", ".join(products[0].keys()))
                print()

        for p in products:
            p["_category_code"] = category_code
            p["_category_name"] = category_name
            yield p

        if page >= total_pages - 1 or not products:
            break
        page += 1
        time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))


def safe_get(d, *keys, default=None):
    for k in keys:
        if d is None:
            return default
        if isinstance(d, dict):
            d = d.get(k)
        elif isinstance(d, list) and isinstance(k, int):
            d = d[k] if k < len(d) else None
        else:
            return default
    return d if d is not None else default


def parse_quantity_label(label):
    """Parse '23,6 cl' of '120 g' of '6 x 100 g' naar (waarde, eenheid).
    Returnt (None, None) als parsen niet lukt."""
    if not label or not isinstance(label, str):
        return None, None
    import re
    # Match: getal (met komma of punt), spatie optioneel, eenheid
    m = re.match(r"^\s*(\d+(?:[,.]\d+)?)\s*([a-zA-Z]+)\s*$", label.strip())
    if m:
        try:
            return float(m.group(1).replace(",", ".")), m.group(2).lower()
        except ValueError:
            return None, None
    # Match: "6 x 100 g" → totaal en eenheid
    m = re.match(r"^\s*(\d+)\s*[xX]\s*(\d+(?:[,.]\d+)?)\s*([a-zA-Z]+)\s*$", label.strip())
    if m:
        try:
            count = int(m.group(1))
            each = float(m.group(2).replace(",", "."))
            return count * each, m.group(3).lower()
        except ValueError:
            return None, None
    return None, None


def parse_price_per_unit(label):
    """Parse '12,67 €/l' naar (12.67, 'l')."""
    if not label or not isinstance(label, str):
        return None, None
    import re
    m = re.search(r"(\d+(?:[,.]\d+)?)\s*[€$£]?/\s*([a-zA-Z]+)", label)
    if m:
        try:
            return float(m.group(1).replace(",", ".")), m.group(2).lower()
        except ValueError:
            return None, None
    return None, None


def parse_product(p):
    """Robuust parsen. Op basis van echte delhaize.be GraphQL response structuur."""
    price_obj = p.get("price") or {}

    # Prijs
    price_val = price_obj.get("value")
    price_cur = price_obj.get("currencyIso") or "EUR"
    discounted_price = None
    discounted_str = price_obj.get("discountedPriceFormatted")
    if discounted_str:
        import re
        m = re.search(r"(\d+[,.]\d+)", discounted_str)
        if m:
            discounted_price = float(m.group(1).replace(",", "."))

    # Prijs per eenheid — zit in supplementaryPriceLabel1
    ppu_val, ppu_unit = parse_price_per_unit(price_obj.get("supplementaryPriceLabel1"))

    # Hoeveelheid in verpakking — zit in supplementaryPriceLabel2
    qty_val, qty_unit = parse_quantity_label(price_obj.get("supplementaryPriceLabel2"))
    qty_label = price_obj.get("supplementaryPriceLabel2")  # ook ruwe string bewaren

    # Image — kies zoom > xlarge > product > rest
    img = None
    images = p.get("images") or []
    if images:
        order = ["zoom", "xlarge", "product", "respListGrid", "small"]
        chosen = None
        for fmt in order:
            for im in images:
                if isinstance(im, dict) and im.get("format") == fmt and im.get("imageType") == "PRIMARY":
                    chosen = im
                    break
            if chosen:
                break
        chosen = chosen or (images[0] if isinstance(images[0], dict) else None)
        if chosen:
            img = chosen.get("url")
            if img and img.startswith("/"):
                img = "https://www.delhaize.be" + img

    # Nutri-Score (zit waarschijnlijk null in listing, komt uit detail)
    nutri = (
        p.get("nutriScoreLetter")
        or p.get("nutriScore")
        or p.get("nutriscore")
    )

    # URL
    url = p.get("url")
    if url and url.startswith("/"):
        url = "https://www.delhaize.be" + url

    # Brand
    brand = p.get("manufacturerName") or p.get("brand")

    # Categorie
    cat_code = (p.get("_category_code")
                or safe_get(p, "firstLevelCategory", "code"))
    cat_name = (p.get("_category_name")
                or safe_get(p, "firstLevelCategory", "name"))

    return {
        "id": p.get("code"),
        "name": p.get("name"),
        "brand": brand,
        "price": price_val,
        "currency": price_cur,
        "discounted_price": discounted_price,
        "price_per_unit_value": ppu_val,
        "price_per_unit_unit": ppu_unit,
        "package_quantity_value": qty_val,
        "package_quantity_unit": qty_unit,
        "package_quantity_label": qty_label,
        "image_url": img,
        "nutri_score": nutri,
        "url": url,
        "category_code": cat_code,
        "category_name": cat_name,
        "in_stock": safe_get(p, "stock", "inStock"),
    }


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY,
            name TEXT,
            brand TEXT,
            category_code TEXT,
            category_name TEXT,
            price REAL,
            currency TEXT,
            discounted_price REAL,
            price_per_unit_value REAL,
            price_per_unit_unit TEXT,
            package_quantity_value REAL,
            package_quantity_unit TEXT,
            package_quantity_label TEXT,
            image_url TEXT,
            nutri_score TEXT,
            url TEXT,
            in_stock INTEGER,
            raw_json TEXT,
            scraped_at TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cat ON products(category_code)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS product_details (
            id TEXT PRIMARY KEY,
            nutri_score TEXT,
            nutrition_per_100 TEXT,
            ingredients TEXT,
            description TEXT,
            allergens TEXT,
            other_info TEXT,
            net_content TEXT,
            raw_json TEXT,
            scraped_at TIMESTAMP,
            FOREIGN KEY (id) REFERENCES products(id)
        )
    """)
    conn.commit()
    return conn


def save_product(conn, raw):
    p = parse_product(raw)
    if not p["id"]:
        return False
    conn.execute("""
        INSERT OR REPLACE INTO products VALUES (
            :id, :name, :brand, :category_code, :category_name,
            :price, :currency, :discounted_price,
            :price_per_unit_value, :price_per_unit_unit,
            :package_quantity_value, :package_quantity_unit, :package_quantity_label,
            :image_url, :nutri_score, :url, :in_stock, :raw, :ts
        )
    """, {**p, "raw": json.dumps(raw, ensure_ascii=False),
          "ts": datetime.utcnow().isoformat()})
    return True


def parse_nutrition_table(ws_nutri_data):
    """Parse de wsNutriFactData.nutrients structuur naar een propere lijst.

    Returns:
        {
          "columns": ["Niet Bereid 100 mlt", "Niet Bereid (%RI) 236MLT"],
          "rows": [
              {"name": "Energie", "values": ["419 KJ", "989 KJ"]},
              {"name": "Kilocalorieën", "values": ["99 KCAL", "234 KCAL"]},
              ...
          ],
          "groups": ["Vitamines", "Mineralen"]  # rijen zonder waarden
        }
    """
    if not ws_nutri_data:
        return None
    nutrient_lists = ws_nutri_data.get("nutrients") or []
    if not nutrient_lists:
        return None
    # Eerste niveau heeft 1 element met 'nutrients' en 'footnote'
    inner = nutrient_lists[0].get("nutrients") or []
    if not inner:
        return None

    columns = []
    rows = []
    groups = []
    for item in inner:
        nid = item.get("id")
        vlist = item.get("valueList") or []
        values = [v.get("value") for v in vlist if isinstance(v, dict)]
        if nid == "Per":
            # Dit is de header-rij met kolomtitels
            columns = values
        elif not values:
            # Lege valueList → groepskop (Vitamines, Mineralen)
            groups.append(nid)
        else:
            rows.append({"name": nid, "values": values})

    return {
        "columns": columns,
        "rows": rows,
        "groups": groups,
        "footnote": nutrient_lists[0].get("footnote"),
    }


def parse_allergens(allergy_list):
    """Parse wsNutriFactData.allegery naar {bevat: [...], onbekend: [...]}"""
    if not allergy_list:
        return None
    result = {}
    for entry in allergy_list:
        title = (entry.get("title") or entry.get("id") or "").lower()
        values = entry.get("values") or []
        if values:
            result[title] = values
    return result if result else None


def parse_other_info(other_info_list):
    """Parse wsNutriFactData.otherInfo naar dict {key: value}"""
    if not other_info_list:
        return None
    result = {}
    for entry in other_info_list:
        key = entry.get("key")
        value = entry.get("value")
        if key and value:
            result[key] = value
    return result if result else None


def parse_details(product_details):
    """Parser voor de ProductDetails GraphQL response (data.productDetails).
    """
    if not product_details:
        return {}

    nutri_data = product_details.get("wsNutriFactData") or {}

    # Voedingstabel
    nutrition = parse_nutrition_table(nutri_data)

    # Allergenen
    allergens = parse_allergens(nutri_data.get("allegery"))  # sic

    # Ingredienten
    ingredients = nutri_data.get("ingredients")

    # Andere info (bewaring, producent, net inhoud, etc.)
    other_info = parse_other_info(nutri_data.get("otherInfo"))

    # Nutri-Score (kan letter A-E zijn of null)
    nutri_score = product_details.get("nutriScoreLetter")

    # Description
    description = product_details.get("description")

    # Net inhoud (uit otherInfo) — vaak preciezer dan in de listing
    net_content = None
    if other_info:
        net_content = (other_info.get("Net inhoud")
                       or other_info.get("Netto inhoud"))

    return {
        "nutri_score": nutri_score,
        "nutrition": nutrition,
        "ingredients": ingredients,
        "allergens": allergens,
        "description": description,
        "other_info": other_info,
        "net_content": net_content,
    }


def save_product_details(conn, product_id, raw_data):
    """Sla productdetail op. raw_data is data uit GraphQL response.

    Werkt de Nutri-Score ook bij in de products tabel als die hier wel ingevuld is.
    """
    detail = (raw_data.get("productDetails")
              or raw_data.get("product")
              or raw_data)
    parsed = parse_details(detail)

    conn.execute("""
        INSERT OR REPLACE INTO product_details
        (id, nutri_score, nutrition_per_100, ingredients, description, allergens,
         other_info, net_content, raw_json, scraped_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        product_id,
        parsed.get("nutri_score"),
        json.dumps(parsed.get("nutrition"), ensure_ascii=False) if parsed.get("nutrition") else None,
        parsed.get("ingredients"),
        parsed.get("description"),
        json.dumps(parsed.get("allergens"), ensure_ascii=False) if parsed.get("allergens") else None,
        json.dumps(parsed.get("other_info"), ensure_ascii=False) if parsed.get("other_info") else None,
        parsed.get("net_content"),
        json.dumps(raw_data, ensure_ascii=False),
        datetime.utcnow().isoformat(),
    ))

    # Update Nutri-Score in products tabel als die nu wel bekend is
    if parsed.get("nutri_score"):
        conn.execute("UPDATE products SET nutri_score = ? WHERE id = ? AND (nutri_score IS NULL OR nutri_score = '')",
                     (parsed.get("nutri_score"), product_id))

    return parsed


KNOWN_CATEGORIES = [
    # ("v2FRU", "Verse groenten en fruit"),
    ("v2DAI", "Zuivel, kaas en plantaardige alternatieven"),
    ("v2CON", "Snel en smakelijk"),
    ("v2APE", "Apero en voorgerechten"),
    ("v2BAK", "Bakkerij en banket"),
    ("v2SWE", "Zoete kruidenierswaren"),
    ("v2SAL", "Zoute kruidenierswaren"),
    ("v2FRO", "Diepvries"),
    ("v2SPE", "Bewuste voeding"),
    ("v2WIN", "Wijn en bubbels"),
    ("v2DRI", "Koude en warme dranken"),
    ("V2ALC", "Bieren, Alcohol en Alcoholvrij"),
    ("v2CLE", "Onderhoud en huishouden"),
    ("v2NON", "Keuken, wonen en vrije tijd"),
    ("v2HYG", "Hygiene en verzorging"),
    ("v2BAB", "Baby"),
    ("v2PET", "Huisdieren"),
    ("v2BBQ", "Barbecue"),
    # ("v2SPO", "Sport & Gezondheid"),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--category", nargs="+", metavar="CODE",
                        help="Een of meer categoriecodes (bv: --category v2DAI v2FRU)")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--cookies", help="Pad naar cookies.txt bestand")
    parser.add_argument("--debug", action="store_true",
                        help="Print eerste product JSON volledig")
    parser.add_argument("--with-details", action="store_true",
                        help="Haal ook voor elk product de detail-call op "
                             "(Nutri-Score, voedingswaarden, ingredienten)")
    parser.add_argument("--details-only", action="store_true",
                        help="Sla listing over, doe alleen detail-calls voor "
                             "producten die al in de DB staan")
    parser.add_argument("--details-limit", type=int, default=None,
                        help="Maximaal aantal detail-calls (handig voor testen)")
    parser.add_argument("--no-sql", action="store_true",
                        help="Sla SQL Server sync over (default: sync automatisch na scrape)")
    parser.add_argument("--sql-mode", choices=["upsert", "replace", "skip"],
                        default="upsert",
                        help="SQL Server sync mode (default: upsert)")
    args = parser.parse_args()

    if not (args.test or args.all or args.category or args.details_only):
        parser.print_help()
        sys.exit(1)

    client = make_client(cookie_file=args.cookies)
    conn = init_db()

    # ---- Details-only mode ----
    if args.details_only:
        print("\n[Details-only] Detail-calls doen voor bestaande producten...")
        rows = conn.execute(
            "SELECT p.id, p.url FROM products p "
            "LEFT JOIN product_details d ON p.id = d.id "
            "WHERE d.id IS NULL"
        ).fetchall()
        print(f"   {len(rows)} producten zonder details")
        done = 0
        for pid, purl in rows:
            try:
                data = fetch_product_details(client, pid, purl, debug=args.debug)
                save_product_details(conn, pid, data)
                done += 1
                if done == 1 and args.debug:
                    print("\n   [DEBUG] Eerste detail response keys:")
                    print("   data keys:", list(data.keys()))
                    print("   sample:", json.dumps(data, indent=2, ensure_ascii=False)[:3000])
                if done % 10 == 0:
                    conn.commit()
                    print(f"     {done} details opgehaald...")
                if args.details_limit and done >= args.details_limit:
                    break
                time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
            except Exception as e:
                print(f"  X Detail-fout voor {pid}: {e}")
        conn.commit()
        print(f"\n[OK] {done} product-details opgeslagen")
        return

    # ---- Normale listing-flow ----
    if args.category:
        # Match codes tegen KNOWN_CATEGORIES voor mooie namen, fallback naar code
        known = dict(KNOWN_CATEGORIES)
        cats = [(c, known.get(c, c)) for c in args.category]
    elif args.test:
        cats = [("v2SPO", "Sport & Gezondheid")]
    else:
        cats = KNOWN_CATEGORIES

    total = 0
    details_done = 0
    for code, name in cats:
        print(f"\n[Categorie] {name} ({code})")
        for prod in scrape_category(client, code, name, debug=args.debug):
            if save_product(conn, prod):
                total += 1

                # Optioneel: meteen details ophalen
                if args.with_details:
                    pid = prod.get("code")
                    purl = prod.get("url")
                    if purl and purl.startswith("/"):
                        purl = "https://www.delhaize.be" + purl
                    try:
                        time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
                        data = fetch_product_details(client, pid, purl, debug=args.debug)
                        save_product_details(conn, pid, data)
                        details_done += 1
                        if details_done == 1 and args.debug:
                            print("\n   [DEBUG] Eerste detail response:")
                            print("   keys:", list(data.keys()))
                            print(json.dumps(data, indent=2, ensure_ascii=False)[:3000])
                            print()
                    except Exception as e:
                        print(f"  X Detail-fout voor {pid}: {e}")

            if total > 0 and total % 25 == 0:
                conn.commit()
                msg = f"     {total} opgeslagen"
                if args.with_details:
                    msg += f" ({details_done} met details)"
                print(msg + "...")
            if args.limit and total >= args.limit:
                break
        conn.commit()

        # === Per-categorie push naar SQL Server ===
        if not args.no_sql:
            print(f"   [Push {code} naar SQL Server]")
            try:
                from sync_to_sqlserver import run_sync
                run_sync(category=[code], mode=args.sql_mode, only="all")
            except ImportError as e:
                print(f"     ! Kan sync_to_sqlserver niet importeren: {e}")
                print("     ! pip install sqlalchemy pyodbc pandas python-dotenv")
                print("     ! Of run met --no-sql om sync over te slaan")
            except Exception as e:
                print(f"     ! Sync gefaald voor {code}: {e}")
                print(f"     ! Data zit nog in delhaize.db, handmatig syncen kan:")
                print(f"         python sync_to_sqlserver.py --category {code}")

        if args.limit and total >= args.limit:
            break

    print(f"\n[OK] {total} producten in {DB_PATH}")
    if args.with_details:
        print(f"     {details_done} product-details opgeslagen")

    # Totalen per categorie
    print("\nProducten per categorie in DB:")
    for row in conn.execute(
        "SELECT category_code, category_name, COUNT(*) FROM products "
        "GROUP BY category_code ORDER BY category_code"
    ):
        print(f"  {row[0]:8} {row[1]:40} {row[2]:>6} producten")

    # Voorbeeld: 2 producten per categorie die we net geraakt hebben
    print("\nVoorbeeld uit DB (2 per categorie):")
    cat_codes = [c[0] for c in cats]
    for code in cat_codes:
        cursor = conn.execute(
            "SELECT name, brand, price, discounted_price, "
            "package_quantity_value, package_quantity_unit, "
            "price_per_unit_value, price_per_unit_unit, nutri_score "
            "FROM products WHERE category_code = ? LIMIT 2",
            (code,)
        )
        for row in cursor:
            name, brand, price, disc, qv, qu, pv, pu, nutri = row
            disc_str = f" (promo EUR{disc})" if disc else ""
            qty_str = f"{qv} {qu}" if qv else "?"
            ppu_str = f"EUR{pv}/{pu}" if pv else "?"
            print(f"  [{code}] [{brand}] {name} | EUR{price}{disc_str} | "
                  f"{qty_str} | {ppu_str} | Nutri: {nutri}")


if __name__ == "__main__":
    main()
