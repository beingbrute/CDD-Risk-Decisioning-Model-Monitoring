"""
Shared test fixtures.

Database tests read the database built by the pipeline. They use the standard PostgreSQL
environment variables (PGHOST, PGPORT, PGUSER, PGPASSWORD; database "cdd") and are skipped
if PGPASSWORD is not set, so the reference-data and calculation tests can always run.
"""
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture(scope="session")
def raw():
    """Read a raw CSV exactly as delivered (text "N/A" stays text)."""
    def _read(name):
        path = RAW / f"{name}.csv"
        if not path.exists():
            pytest.skip(f"{path.name} not found; run src/generate_data.py first")
        return pd.read_csv(path, keep_default_na=False, na_values=[""])
    return _read


@pytest.fixture(scope="session")
def db():
    if not os.environ.get("PGPASSWORD"):
        pytest.skip("PGPASSWORD not set; database tests skipped")
    import psycopg2
    con = psycopg2.connect(host=os.environ.get("PGHOST", "localhost"), port=os.environ.get("PGPORT", "5432"),
                           user=os.environ.get("PGUSER", "postgres"), password=os.environ["PGPASSWORD"],
                           dbname=os.environ.get("PGDATABASE", "cdd"))
    yield con
    con.close()


@pytest.fixture(scope="session")
def q(db):
    """Run a query and return the first column of the first row."""
    def _q(sql, params=None):
        with db.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return row[0] if row else None
    return _q
