"""
SQLAlchemy database helper voor Delhaize scraper.
Volgt het patroon van je bestaande database.py.

Configureer via .env (zie .env.example).
"""
import logging
import os
import urllib

from sqlalchemy import create_engine, text
from dotenv import load_dotenv
import pandas as pd

logger = logging.getLogger(__name__)

load_dotenv()

# Config voor database
SERVER = os.getenv("DB_SERVER", "127.0.0.1,1433")
DATABASE = os.getenv("DB_NAME", "Delhaize")
DRIVER = os.getenv("DB_DRIVER", "ODBC Driver 17 for SQL Server")
USER = os.getenv("DB_USER", "sa")
PASSWORD = os.getenv("databasePWD")
USE_WINDOWS_AUTH = os.getenv("DB_WINDOWS_AUTH", "false").lower() in ("1", "true", "yes")


def get_engine():
    """Maakt een SQLAlchemy engine."""
    if USE_WINDOWS_AUTH:
        conn_str = (
            f"DRIVER={{{DRIVER}}};"
            f"SERVER={SERVER};"
            f"DATABASE={DATABASE};"
            "Trusted_Connection=yes;"
            "TrustServerCertificate=yes;"
        )
    else:
        conn_str = (
            f"DRIVER={{{DRIVER}}};"
            f"SERVER={SERVER};"
            f"DATABASE={DATABASE};"
            f"UID={USER};"
            f"PWD={PASSWORD};"
            "Encrypt=yes;"
            "TrustServerCertificate=yes;"
        )
    quoted_conn_str = urllib.parse.quote_plus(conn_str)
    engine = create_engine(
        f"mssql+pyodbc:///?odbc_connect={quoted_conn_str}",
    )
    return engine


def loadIN(engine, df=None, table=None, if_exists="append"):
    """Laad DataFrame in SQL tabel."""
    if df is None or table is None:
        raise ValueError("df en table zijn verplicht")
    try:
        df.to_sql(
            name=table,
            con=engine,
            if_exists=if_exists,
            index=False,
            chunksize=500,
        )
        logger.info("Succes: %d rijen geladen in %s", len(df), table)
    except Exception as e:
        logger.exception("Fout bij laden in %s: %s", table, e)
        raise e


def getData(engine, query=None):
    """Haal data op uit database."""
    if query is None:
        raise ValueError("Een SQL-query is verplicht")
    try:
        return pd.read_sql(query, con=engine)
    except Exception as e:
        print(f"Fout bij ophalen: {e}")
        return None


def executeSQL(engine, query=None, params=None):
    """Voer een DML/DDL SQL statement uit (INSERT/UPDATE/DELETE/MERGE/CREATE)."""
    if query is None:
        raise ValueError("Een SQL-query is verplicht")
    try:
        with engine.begin() as conn:
            result = conn.execute(text(query), params or {})
            return result.rowcount
    except Exception as e:
        logger.exception("Fout bij executeSQL: %s", e)
        raise e
