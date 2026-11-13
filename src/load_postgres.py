"""
Load the raw CSV files into PostgreSQL, unchanged.

Creates the database `cdd` (if it does not exist) with three schemas: raw, staging and
analytics. This script fills `raw`, with one table per CSV file. Every column is loaded
as TEXT, so bad values such as "Hgh" or a negative amount arrive exactly as they are in
the file. Cleaning and type conversion happen in
the next layer (staging), where the data-quality checks can see the original values.

Usage:
    python src\\load_postgres.py
    python src\\load_postgres.py --user postgres --host localhost --port 5432

The password is asked for when the script runs and is not stored anywhere. To avoid
typing it each time, set the PGPASSWORD environment variable in your own session.
"""
import argparse
import csv
import getpass
import os
import sys
from pathlib import Path

import psycopg2
from psycopg2 import sql

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
DB_NAME = "cdd"
TABLES = ["country_risk_history", "cdd_rules", "customers", "company_shareholders", "documents",
          "monthly_activity", "review_cases", "expected_issues"]


def connect(args, dbname):
    return psycopg2.connect(host=args.host, port=args.port, user=args.user,
                            password=args.password, dbname=dbname)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", default="5432")
    p.add_argument("--user", default="postgres")
    args = p.parse_args()
    args.password = os.environ.get("PGPASSWORD") or getpass.getpass(f"PostgreSQL password for {args.user}: ")

    missing = [t for t in TABLES if not (RAW / f"{t}.csv").exists()]
    if missing:
        print(f"ERROR: missing files in data/raw: {missing}. Run src/generate_data.py first.")
        sys.exit(2)

    # 1. create the database if needed
    try:
        con = connect(args, "postgres")
    except psycopg2.OperationalError as e:
        print(f"ERROR: could not connect to PostgreSQL: {e}")
        sys.exit(2)
    con.autocommit = True
    with con.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,))
        if cur.fetchone() is None:
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DB_NAME)))
            print(f"Created database {DB_NAME}")
    con.close()

    # 2. load each CSV into raw.<table>, replacing any earlier load
    con = connect(args, DB_NAME)
    results = []
    with con, con.cursor() as cur:
        # raw = files as delivered; staging = typed and cleaned; analytics = rules, queues, model inputs
        for schema in ("raw", "staging", "analytics"):
            cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        for t in TABLES:
            path = RAW / f"{t}.csv"
            with open(path, newline="", encoding="utf-8") as f:
                header = next(csv.reader(f))
                file_rows = sum(1 for _ in f)
            cols = sql.SQL(", ").join(sql.SQL("{} TEXT").format(sql.Identifier(c)) for c in header)
            tbl = sql.Identifier("raw", t)
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(tbl))
            cur.execute(sql.SQL("CREATE TABLE {} ({})").format(tbl, cols))
            with open(path, newline="", encoding="utf-8") as f:
                cur.copy_expert(sql.SQL("COPY {} FROM STDIN WITH (FORMAT csv, HEADER true)").format(tbl).as_string(cur), f)
            cur.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(tbl))
            db_rows = cur.fetchone()[0]
            results.append((t, file_rows, db_rows))
    con.close()

    print(f"\n{'table':26s} {'csv rows':>9s} {'db rows':>9s}  check")
    ok = True
    for t, fr, dr in results:
        match = fr == dr
        ok &= match
        print(f"raw.{t:22s} {fr:>9,d} {dr:>9,d}  {'OK' if match else 'MISMATCH'}")
    print("\nAll tables loaded and row counts match." if ok else "\nERROR: row counts do not match.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
