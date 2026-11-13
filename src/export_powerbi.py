"""
Export the tables the Power BI dashboard uses from PostgreSQL to data/powerbi/*.csv.

Power BI reads these CSV files, so the dashboard opens without a database connection.
Re-run this script after any pipeline change, then press Refresh in Power BI.

Usage:
    python src\\export_powerbi.py --port 5433
"""
import argparse
import getpass
import os
import sys
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "powerbi"

QUERIES = {
    # ---------- page 1: data quality ----------
    "dq_summary": """
        SELECT check_name, table_name, severity, records_failed, status, description
        FROM analytics.dq_summary ORDER BY check_name""",
    "dq_answer_key_test": """
        SELECT check_name, expected, found, matched, missed, extra
        FROM analytics.dq_answer_key_test ORDER BY check_name""",
    "dq_exceptions": """
        SELECT e.check_name, k.severity, k.table_name, e.record_key, e.detail
        FROM analytics.dq_exceptions e JOIN analytics.dq_check_catalogue k USING (check_name)
        ORDER BY e.check_name, e.record_key""",

    # ---------- page 2: review queue ----------
    "review_queue": """
        SELECT q.queue_position, q.customer_id, q.priority, q.priority_score, q.rules_hit,
               q.reason_codes, q.next_actions, q.days_overdue, q.risk_rating,
               q.customer_type, q.country_code, COALESCE(r.country_name, q.country_code) AS country_name,
               q.channel, q.last_review_date, q.next_review_due
        FROM analytics.review_queue q
        LEFT JOIN (SELECT DISTINCT country_code, country_name FROM staging.country_risk_history) r
               USING (country_code)
        ORDER BY q.queue_position""",
    "queue_reasons": """
        SELECT h.customer_id, h.rule_code, r.description AS rule_description, r.next_action,
               r.basis_type, h.detail, q.priority
        FROM analytics.rule_hits h
        JOIN staging.cdd_rules r USING (rule_code)
        JOIN analytics.review_queue q USING (customer_id)
        WHERE h.eval_type = 'PORTFOLIO'
        ORDER BY h.customer_id, h.rule_code""",
    "cdd_rules": """
        SELECT r.rule_code, r.description, r.next_action, r.legal_basis, r.basis_type, w.weight
        FROM staging.cdd_rules r LEFT JOIN analytics.rule_weights w USING (rule_code)
        ORDER BY r.rule_code""",
    "rule_effectiveness": """
        SELECT rule_code, description, cases_flagged, escalated_flagged, flag_rate,
               precision, recall, lift
        FROM analytics.rule_effectiveness ORDER BY rule_code""",
    "fatf_status_history": """
        SELECT country_code, country_name, list_version, fatf_status, uk_high_risk_third_country,
               effective_from, effective_to, source
        FROM staging.country_risk_history
        WHERE country_code IN (SELECT country_code FROM staging.country_risk_history
                               WHERE fatf_status <> 'Not listed')
        ORDER BY country_name, list_version""",

    # ---------- page 3: model and monitoring ----------
    "model_vs_rules": """
        SELECT period, approach,
               COUNT(*) AS cases,
               SUM(flag) AS cases_flagged,
               ROUND(AVG(flag), 3) AS workload,
               ROUND(SUM(flag * analyst_escalated)::numeric / NULLIF(SUM(flag), 0), 3) AS precision,
               ROUND(SUM(flag * analyst_escalated)::numeric / NULLIF(SUM(analyst_escalated), 0), 3) AS recall,
               MIN(review_date) AS period_start
        FROM (SELECT period, review_date, analyst_escalated, 'Model' AS approach, flagged AS flag
              FROM analytics.model_scores
              UNION ALL
              SELECT period, review_date, analyst_escalated, 'Rules process', any_rule
              FROM analytics.model_scores) x
        GROUP BY period, approach
        ORDER BY MIN(review_date), approach""",
    "monitoring_status": """
        SELECT month, period, auc, auc_vs_validation, max_portfolio_psi, max_model_input_psi, score_psi,
               breach::int AS breach, watch::int AS watch, status
        FROM analytics.monitoring_status ORDER BY month""",
    "monitoring_psi": "SELECT * FROM analytics.monitoring_psi ORDER BY source, variable, month",
    "monitoring_performance": "SELECT * FROM analytics.monitoring_performance ORDER BY month",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", default=os.environ.get("PGPORT", "5432"))
    ap.add_argument("--user", default="postgres")
    ap.add_argument("--db", default="cdd")
    args = ap.parse_args()
    password = os.environ.get("PGPASSWORD") or getpass.getpass(f"PostgreSQL password for {args.user}: ")
    try:
        con = psycopg2.connect(host=args.host, port=args.port, user=args.user, password=password, dbname=args.db)
    except psycopg2.OperationalError as e:
        print(f"ERROR: could not connect to PostgreSQL: {e}")
        sys.exit(2)

    OUT.mkdir(parents=True, exist_ok=True)
    with con, con.cursor() as cur:
        for name, q in QUERIES.items():
            path = OUT / f"{name}.csv"
            with open(path, "w", encoding="utf-8", newline="") as f:
                cur.copy_expert(f"COPY ({q}) TO STDOUT WITH (FORMAT csv, HEADER true)", f)
            rows = sum(1 for _ in open(path, encoding="utf-8")) - 1
            print(f"{name + '.csv':28s} {rows:>6,d} rows")
    con.close()
    print(f"\nSaved to {OUT}")


if __name__ == "__main__":
    main()
