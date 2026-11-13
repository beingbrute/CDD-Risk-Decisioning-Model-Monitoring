"""
Run SQL files against the cdd database, in order, and print the result of the last
query in each file.

Usage:
    python src\\run_sql.py --port 5433 sql\\01_staging.sql sql\\02_dq_checks.sql
    python src\\run_sql.py --port 5433            (runs every file in sql\\ in name order)

The password is asked for when the script runs and is not stored anywhere
(or set PGPASSWORD in your own session). Exit code 0 = all files ran, 1 = a file failed,
2 = connection or file problem.
"""
import argparse
import getpass
import os
import sys
import time
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[1]


def print_table(cols, rows):
    rows = [["" if v is None else str(v) for v in r] for r in rows]
    widths = [max(len(c), *(len(r[i]) for r in rows)) if rows else len(c) for i, c in enumerate(cols)]
    line = "  ".join(c.ljust(w) for c, w in zip(cols, widths))
    print(line)
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(v.ljust(w) for v, w in zip(r, widths)))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("files", nargs="*", help="SQL files to run (default: all files in sql/)")
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", default=os.environ.get("PGPORT", "5432"))
    p.add_argument("--user", default="postgres")
    p.add_argument("--db", default="cdd")
    args = p.parse_args()

    files = [Path(f) for f in args.files] or sorted((ROOT / "sql").glob("*.sql"))
    missing = [str(f) for f in files if not f.exists()]
    if missing:
        print(f"ERROR: file(s) not found: {missing}")
        sys.exit(2)

    password = os.environ.get("PGPASSWORD") or getpass.getpass(f"PostgreSQL password for {args.user}: ")
    try:
        con = psycopg2.connect(host=args.host, port=args.port, user=args.user,
                               password=password, dbname=args.db)
    except psycopg2.OperationalError as e:
        print(f"ERROR: could not connect to PostgreSQL: {e}")
        sys.exit(2)

    for f in files:
        print(f"\n=== {f.name} ===")
        start = time.time()
        try:
            with con, con.cursor() as cur:             # one transaction per file
                cur.execute(f.read_text(encoding="utf-8"))
                if cur.description:                  # the file's last statement returned rows
                    print_table([d[0] for d in cur.description], cur.fetchall())
        except psycopg2.Error as e:
            print(f"FAILED: {e}")
            con.close()
            sys.exit(1)
        print(f"({time.time() - start:.1f} s)")
    con.close()


if __name__ == "__main__":
    main()
