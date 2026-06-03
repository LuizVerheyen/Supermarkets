"""
Importeer producten.json (Colruyt scrape) in de Delhaize SQL Server database.

Hergebruikt jouw database.py (get_engine / loadIN / executeSQL) en de
staging+MERGE-logica uit sync_to_sqlserver.py. Volledig idempotent (upsert).

GEBRUIK:
    python import_colruyt_json.py
    python import_colruyt_json.py --json ../producten.json --mode upsert
    python import_colruyt_json.py --mode replace   # eerst alles wissen

VOORWAARDEN:
    - .env staat goed (DB_SERVER, DB_NAME, DB_WINDOWS_AUTH=true)
    - De Delhaize-database + tabellen bestaan al
    - pip install sqlalchemy pyodbc pandas python-dotenv
"""

import argparse
import json
import logging
import os

import pandas as pd

from database import get_engine, executeSQL, getData
from sync_to_sqlserver import sync_products

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_JSON = os.path.join(os.path.dirname(__file__), "..", "producten.json")

# Kolomvolgorde zoals de ProductsStaging-tabel verwacht
PRODUCT_COLUMNS = [
    "ProductId", "Name", "Brand", "CategoryCode", "Price", "Currency",
    "DiscountedPrice", "PricePerUnitValue", "PricePerUnitUnit",
    "PackageQuantityValue", "PackageQuantityUnit", "PackageQuantityLabel",
    "ImageUrl", "NutriScore", "Url", "InStock",
]


def _f(v):
    """Naar float of None."""
    try:
        return float(v) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _trunc(v, n):
    return None if v is None else str(v)[:n]


def map_product(p: dict) -> dict:
    price = p.get("price") or {}
    cid = p.get("topCategoryId")
    ns = p.get("nutriscoreLabel")
    basic = _f(price.get("basicPrice"))
    pid = p.get("productId")
    return {
        "ProductId": _trunc(pid, 50),
        "Name": _trunc(p.get("name") or p.get("LongName"), 500),
        "Brand": _trunc(p.get("brand"), 200),
        "CategoryCode": _trunc(cid, 20) if cid else None,
        "Price": basic,
        "Currency": "EUR",
        # alleen een korting tonen bij een actieve promo (geen apart bedrag in de data)
        "DiscountedPrice": basic if (p.get("inPromo") and price.get("isPromoActive") == "Y") else None,
        "PricePerUnitValue": _f(price.get("measurementUnitPrice")),
        "PricePerUnitUnit": _trunc(price.get("measurementUnit"), 20),
        "PackageQuantityValue": _f(p.get("amount")),
        "PackageQuantityUnit": _trunc(p.get("amountUnit"), 20),
        "PackageQuantityLabel": _trunc(p.get("content"), 100),
        "ImageUrl": _trunc(p.get("fullImage"), 1000),
        "NutriScore": ns[:1].upper() if ns else None,
        "Url": _trunc(f"https://www.colruyt.be/nl/producten/{pid}", 1000) if pid else None,
        "InStock": 1 if p.get("isAvailable") else 0,
    }


def build_products_df(products: list) -> pd.DataFrame:
    df = pd.DataFrame([map_product(p) for p in products], columns=PRODUCT_COLUMNS)
    df = df.drop_duplicates(subset="ProductId", keep="last")
    df["InStock"] = df["InStock"].astype("Int64")
    return df


def collect_categories(products: list) -> dict:
    cats = {}
    for p in products:
        cid = p.get("topCategoryId")
        if cid:
            cats[str(cid)[:20]] = _trunc(p.get("topCategoryName") or "", 200)
    return cats


def sync_categories_with_names(engine, cats: dict):
    """Categorieen upsserten met hun echte naam (FK voor Products)."""
    for code, name in cats.items():
        executeSQL(engine, """
            MERGE dbo.Categories AS target
            USING (SELECT :code AS CategoryCode, :name AS CategoryName) AS src
                ON target.CategoryCode = src.CategoryCode
            WHEN MATCHED THEN UPDATE SET CategoryName = src.CategoryName, LastScrapedAt = SYSUTCDATETIME()
            WHEN NOT MATCHED THEN INSERT (CategoryCode, CategoryName, LastScrapedAt)
                VALUES (src.CategoryCode, src.CategoryName, SYSUTCDATETIME());
        """, {"code": code, "name": name})
    logger.info("Categories: %d gesynct", len(cats))


def main():
    parser = argparse.ArgumentParser(description="Import Colruyt producten.json -> Delhaize DB")
    parser.add_argument("--json", default=DEFAULT_JSON)
    parser.add_argument("--mode", choices=["upsert", "replace", "skip"], default="upsert")
    args = parser.parse_args()

    with open(args.json, encoding="utf-8") as f:
        products = json.load(f)
    logger.info("%d producten gelezen uit %s", len(products), args.json)

    df = build_products_df(products)
    cats = collect_categories(products)
    logger.info("%d unieke producten, %d categorieen", len(df), len(cats))

    engine = get_engine()
    sync_categories_with_names(engine, cats)
    sync_products(engine, df, mode=args.mode)

    overview = getData(engine, """
        SELECT c.CategoryCode, c.CategoryName, COUNT(p.ProductId) AS Products
        FROM dbo.Categories c
        LEFT JOIN dbo.Products p ON p.CategoryCode = c.CategoryCode
        GROUP BY c.CategoryCode, c.CategoryName
        HAVING COUNT(p.ProductId) > 0
        ORDER BY Products DESC
    """)
    if overview is not None and not overview.empty:
        logger.info("\n%s", overview.to_string(index=False))
        logger.info("TOTAAL producten in DB: %d", overview["Products"].sum())


if __name__ == "__main__":
    main()
