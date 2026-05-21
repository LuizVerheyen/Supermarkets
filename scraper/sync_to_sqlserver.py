"""
Sync delhaize.db (SQLite) -> SQL Server via SQLAlchemy.

Gebruikt staging tables + MERGE voor snelle UPSERTs (10-50x sneller dan
row-by-row MERGE bij grote volumes).

INSTALLATIE:
    pip install sqlalchemy pyodbc pandas python-dotenv

GEBRUIK:
    # Sync alles uit delhaize.db
    python sync_to_sqlserver.py

    # Sync 1 of meer specifieke categorieen
    python sync_to_sqlserver.py --category v2DAI
    python sync_to_sqlserver.py --category v2DAI v2FRU v2SAL

    # Mode kiezen
    python sync_to_sqlserver.py --mode upsert      # default
    python sync_to_sqlserver.py --mode replace     # categorie eerst leegmaken
    python sync_to_sqlserver.py --mode skip        # bestaande negeren

    # Alleen 1 onderdeel syncen
    python sync_to_sqlserver.py --only products
    python sync_to_sqlserver.py --only details

VOORWAARDEN:
    1. Voer schema.sql uit in SSMS (maakt database + tabellen aan)
    2. Maak .env aan op basis van .env.example
"""

import argparse
import logging
import sqlite3

import pandas as pd

from database import get_engine, loadIN, executeSQL, getData

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SQLITE_DB = "delhaize.db"

KNOWN_CATEGORIES = [
    ("v2FRU", "Verse groenten en fruit"),
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
    ("v2SPO", "Sport & Gezondheid"),
]


# =============================================================================
# DATA LADEN UIT SQLITE
# =============================================================================

def load_products_df(category_filter=None):
    conn = sqlite3.connect(SQLITE_DB)
    sql = """
        SELECT
            id                      AS ProductId,
            name                    AS Name,
            brand                   AS Brand,
            category_code           AS CategoryCode,
            price                   AS Price,
            currency                AS Currency,
            discounted_price        AS DiscountedPrice,
            price_per_unit_value    AS PricePerUnitValue,
            price_per_unit_unit     AS PricePerUnitUnit,
            package_quantity_value  AS PackageQuantityValue,
            package_quantity_unit   AS PackageQuantityUnit,
            package_quantity_label  AS PackageQuantityLabel,
            image_url               AS ImageUrl,
            nutri_score             AS NutriScore,
            url                     AS Url,
            in_stock                AS InStock
        FROM products
    """
    params = []
    if category_filter:
        placeholders = ",".join("?" * len(category_filter))
        sql += f" WHERE category_code IN ({placeholders})"
        params = list(category_filter)
    df = pd.read_sql(sql, conn, params=params)
    conn.close()
    # InStock naar nullable Int (pyodbc accepteert dat als BIT)
    if "InStock" in df.columns:
        df["InStock"] = df["InStock"].astype("Int64")
    return df


def load_details_df(category_filter=None):
    conn = sqlite3.connect(SQLITE_DB)
    sql = """
        SELECT
            d.id                  AS ProductId,
            d.nutri_score         AS NutriScore,
            d.nutrition_per_100   AS NutritionPer100,
            d.ingredients         AS Ingredients,
            d.description         AS Description,
            d.allergens           AS Allergens,
            d.other_info          AS OtherInfo,
            d.net_content         AS NetContent
        FROM product_details d
        JOIN products p ON d.id = p.id
    """
    params = []
    if category_filter:
        placeholders = ",".join("?" * len(category_filter))
        sql += f" WHERE p.category_code IN ({placeholders})"
        params = list(category_filter)
    df = pd.read_sql(sql, conn, params=params)
    conn.close()
    return df


# =============================================================================
# CATEGORIES SYNC
# =============================================================================

def sync_categories(engine):
    for code, name in KNOWN_CATEGORIES:
        executeSQL(engine, """
            MERGE dbo.Categories AS target
            USING (SELECT :code AS CategoryCode, :name AS CategoryName) AS src
                ON target.CategoryCode = src.CategoryCode
            WHEN MATCHED THEN
                UPDATE SET CategoryName = src.CategoryName
            WHEN NOT MATCHED THEN
                INSERT (CategoryCode, CategoryName) VALUES (src.CategoryCode, src.CategoryName);
        """, {"code": code, "name": name})
    logger.info("Categories: %d rijen gesynct", len(KNOWN_CATEGORIES))


# =============================================================================
# PRODUCTS SYNC (staging table + MERGE)
# =============================================================================

def sync_products(engine, df, mode="upsert", categories_to_replace=None):
    if df.empty:
        logger.info("Products: niets te syncen")
        return 0

    if mode == "replace":
        if categories_to_replace:
            placeholders = ",".join(f"'{c}'" for c in categories_to_replace)
            executeSQL(engine, f"""
                DELETE FROM dbo.ProductDetails WHERE ProductId IN (
                    SELECT ProductId FROM dbo.Products WHERE CategoryCode IN ({placeholders})
                );
                DELETE FROM dbo.Products WHERE CategoryCode IN ({placeholders});
            """)
            logger.info("Replace: producten van %d cat(s) gewist", len(categories_to_replace))
        else:
            executeSQL(engine, "DELETE FROM dbo.ProductDetails; DELETE FROM dbo.Products;")
            logger.info("Replace: alle producten gewist")

    # Zorg dat alle CategoryCodes uit df bestaan in Categories (FK)
    needed = df["CategoryCode"].dropna().unique().tolist()
    if needed:
        values_sql = ",".join(f"('{c}')" for c in needed)
        executeSQL(engine, f"""
            INSERT INTO dbo.Categories (CategoryCode, CategoryName)
            SELECT v.code, v.code
            FROM (VALUES {values_sql}) AS v(code)
            WHERE NOT EXISTS (SELECT 1 FROM dbo.Categories c WHERE c.CategoryCode = v.code);
        """)

    staging = "ProductsStaging"
    executeSQL(engine, f"""
        IF OBJECT_ID('dbo.{staging}', 'U') IS NOT NULL DROP TABLE dbo.{staging};
        CREATE TABLE dbo.{staging} (
            ProductId NVARCHAR(50) NOT NULL,
            Name NVARCHAR(500), Brand NVARCHAR(200), CategoryCode NVARCHAR(20),
            Price DECIMAL(10,2), Currency NVARCHAR(10), DiscountedPrice DECIMAL(10,2),
            PricePerUnitValue DECIMAL(10,4), PricePerUnitUnit NVARCHAR(20),
            PackageQuantityValue DECIMAL(10,3), PackageQuantityUnit NVARCHAR(20),
            PackageQuantityLabel NVARCHAR(100),
            ImageUrl NVARCHAR(1000), NutriScore NCHAR(1),
            Url NVARCHAR(1000), InStock BIT
        );
    """)

    logger.info("Products: bulk load %d rijen naar staging...", len(df))
    loadIN(engine, df=df, table=staging, if_exists="append")

    if mode == "skip":
        merge_sql = f"""
        INSERT INTO dbo.Products (
            ProductId, Name, Brand, CategoryCode, Price, Currency, DiscountedPrice,
            PricePerUnitValue, PricePerUnitUnit,
            PackageQuantityValue, PackageQuantityUnit, PackageQuantityLabel,
            ImageUrl, NutriScore, Url, InStock
        )
        SELECT s.ProductId, s.Name, s.Brand, s.CategoryCode, s.Price, s.Currency, s.DiscountedPrice,
               s.PricePerUnitValue, s.PricePerUnitUnit,
               s.PackageQuantityValue, s.PackageQuantityUnit, s.PackageQuantityLabel,
               s.ImageUrl, s.NutriScore, s.Url, s.InStock
        FROM dbo.{staging} s
        WHERE NOT EXISTS (SELECT 1 FROM dbo.Products p WHERE p.ProductId = s.ProductId);
        """
    else:
        merge_sql = f"""
        MERGE dbo.Products AS target
        USING dbo.{staging} AS src
            ON target.ProductId = src.ProductId
        WHEN MATCHED THEN UPDATE SET
            Name = src.Name, Brand = src.Brand, CategoryCode = src.CategoryCode,
            Price = src.Price, Currency = src.Currency, DiscountedPrice = src.DiscountedPrice,
            PricePerUnitValue = src.PricePerUnitValue, PricePerUnitUnit = src.PricePerUnitUnit,
            PackageQuantityValue = src.PackageQuantityValue,
            PackageQuantityUnit = src.PackageQuantityUnit,
            PackageQuantityLabel = src.PackageQuantityLabel,
            ImageUrl = src.ImageUrl, NutriScore = src.NutriScore,
            Url = src.Url, InStock = src.InStock,
            ScrapedAt = SYSUTCDATETIME()
        WHEN NOT MATCHED THEN INSERT (
            ProductId, Name, Brand, CategoryCode, Price, Currency, DiscountedPrice,
            PricePerUnitValue, PricePerUnitUnit,
            PackageQuantityValue, PackageQuantityUnit, PackageQuantityLabel,
            ImageUrl, NutriScore, Url, InStock
        ) VALUES (
            src.ProductId, src.Name, src.Brand, src.CategoryCode, src.Price, src.Currency,
            src.DiscountedPrice, src.PricePerUnitValue, src.PricePerUnitUnit,
            src.PackageQuantityValue, src.PackageQuantityUnit, src.PackageQuantityLabel,
            src.ImageUrl, src.NutriScore, src.Url, src.InStock
        );
        """
    executeSQL(engine, merge_sql)
    executeSQL(engine, f"DROP TABLE dbo.{staging};")
    logger.info("Products: %d rijen verwerkt", len(df))

    if needed:
        placeholders = ",".join(f"'{c}'" for c in needed)
        executeSQL(engine, f"""
            UPDATE c SET
                LastScrapedAt = SYSUTCDATETIME(),
                ProductCount = (SELECT COUNT(*) FROM dbo.Products p WHERE p.CategoryCode = c.CategoryCode)
            FROM dbo.Categories c
            WHERE c.CategoryCode IN ({placeholders});
        """)
    return len(df)


# =============================================================================
# PRODUCT DETAILS SYNC
# =============================================================================

def sync_product_details(engine, df, mode="upsert"):
    if df.empty:
        logger.info("ProductDetails: niets te syncen")
        return 0

    staging = "ProductDetailsStaging"
    executeSQL(engine, f"""
        IF OBJECT_ID('dbo.{staging}', 'U') IS NOT NULL DROP TABLE dbo.{staging};
        CREATE TABLE dbo.{staging} (
            ProductId NVARCHAR(50) NOT NULL,
            NutriScore NCHAR(1) NULL,
            NutritionPer100 NVARCHAR(MAX) NULL,
            Ingredients NVARCHAR(MAX) NULL,
            Description NVARCHAR(MAX) NULL,
            Allergens NVARCHAR(MAX) NULL,
            OtherInfo NVARCHAR(MAX) NULL,
            NetContent NVARCHAR(100) NULL
        );
    """)

    logger.info("ProductDetails: bulk load %d rijen naar staging...", len(df))
    loadIN(engine, df=df, table=staging, if_exists="append")

    if mode == "skip":
        sql = f"""
        INSERT INTO dbo.ProductDetails (
            ProductId, NutriScore, NutritionPer100, Ingredients, Description,
            Allergens, OtherInfo, NetContent
        )
        SELECT s.ProductId, s.NutriScore, s.NutritionPer100, s.Ingredients, s.Description,
               s.Allergens, s.OtherInfo, s.NetContent
        FROM dbo.{staging} s
        WHERE EXISTS (SELECT 1 FROM dbo.Products p WHERE p.ProductId = s.ProductId)
          AND NOT EXISTS (SELECT 1 FROM dbo.ProductDetails d WHERE d.ProductId = s.ProductId);
        """
    else:
        sql = f"""
        MERGE dbo.ProductDetails AS target
        USING (
            SELECT s.* FROM dbo.{staging} s
            WHERE EXISTS (SELECT 1 FROM dbo.Products p WHERE p.ProductId = s.ProductId)
        ) AS src
            ON target.ProductId = src.ProductId
        WHEN MATCHED THEN UPDATE SET
            NutriScore = src.NutriScore,
            NutritionPer100 = src.NutritionPer100,
            Ingredients = src.Ingredients,
            Description = src.Description,
            Allergens = src.Allergens,
            OtherInfo = src.OtherInfo,
            NetContent = src.NetContent,
            ScrapedAt = SYSUTCDATETIME()
        WHEN NOT MATCHED THEN INSERT (
            ProductId, NutriScore, NutritionPer100, Ingredients, Description,
            Allergens, OtherInfo, NetContent
        ) VALUES (
            src.ProductId, src.NutriScore, src.NutritionPer100, src.Ingredients,
            src.Description, src.Allergens, src.OtherInfo, src.NetContent
        );
        """
    executeSQL(engine, sql)

    # Propagate Nutri-Score naar Products tabel
    executeSQL(engine, f"""
        UPDATE p SET p.NutriScore = s.NutriScore
        FROM dbo.Products p
        JOIN dbo.{staging} s ON p.ProductId = s.ProductId
        WHERE s.NutriScore IS NOT NULL
          AND (p.NutriScore IS NULL OR p.NutriScore = '');
    """)
    executeSQL(engine, f"DROP TABLE dbo.{staging};")
    logger.info("ProductDetails: %d rijen verwerkt", len(df))
    return len(df)


# =============================================================================
# ENTRY POINTS
# =============================================================================

def run_sync(category=None, mode="upsert", only="all"):
    """Hoofdfunctie - kan ook geimporteerd worden vanuit de scraper."""
    engine = get_engine()

    if only in ("categories", "all"):
        logger.info("[Categories]")
        sync_categories(engine)

    if only in ("products", "all"):
        logger.info("[Products]")
        df_p = load_products_df(category_filter=category)
        sync_products(engine, df_p, mode=mode, categories_to_replace=category)

    if only in ("details", "all"):
        logger.info("[ProductDetails]")
        df_d = load_details_df(category_filter=category)
        sync_product_details(engine, df_d, mode=mode)

    overview = getData(engine, """
        SELECT c.CategoryCode, c.CategoryName,
               COUNT(p.ProductId) AS Products,
               SUM(CASE WHEN d.ProductId IS NOT NULL THEN 1 ELSE 0 END) AS WithDetails
        FROM dbo.Categories c
        LEFT JOIN dbo.Products p ON p.CategoryCode = c.CategoryCode
        LEFT JOIN dbo.ProductDetails d ON d.ProductId = p.ProductId
        GROUP BY c.CategoryCode, c.CategoryName
        HAVING COUNT(p.ProductId) > 0
        ORDER BY c.CategoryCode
    """)
    if overview is not None and not overview.empty:
        logger.info("\n%s", overview.to_string(index=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["upsert", "replace", "skip"], default="upsert")
    parser.add_argument("--category", nargs="+", metavar="CODE")
    parser.add_argument("--only", choices=["categories", "products", "details", "all"],
                        default="all")
    args = parser.parse_args()
    run_sync(category=args.category, mode=args.mode, only=args.only)


if __name__ == "__main__":
    main()
