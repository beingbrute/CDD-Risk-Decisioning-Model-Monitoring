"""
End-to-end checks on the database built by the pipeline.

Several tests recompute a result independently in pandas from the raw CSV files and
compare it with what the SQL produced, so the SQL is not only checking itself.
"""
import numpy as np
import pandas as pd
import pytest

AS_OF = pd.Timestamp("2025-12-31")


# ---------------------------------------------------------------- loading and staging
@pytest.mark.parametrize("table", ["customers", "company_shareholders", "documents",
                                   "monthly_activity", "review_cases", "country_risk_history"])
def test_raw_and_staging_row_counts_match_the_files(q, raw, table):
    n_file = len(raw(table))
    assert q(f"SELECT COUNT(*) FROM raw.{table}") == n_file
    stg = "customers_all" if table == "customers" else table
    assert q(f"SELECT COUNT(*) FROM staging.{stg}") == n_file


def test_invalid_ratings_are_not_auto_corrected(q):
    # "LOW " and "medium" must stay invalid, not be fixed to Low / Medium
    assert q("SELECT COUNT(*) FROM staging.customers WHERE risk_rating_raw IN ('LOW ', 'medium') "
             "AND risk_rating IS NULL") == 2


# ---------------------------------------------------------------- data-quality checks
def test_dq_checks_find_exactly_the_planted_issues(q):
    assert q("SELECT SUM(expected) FROM analytics.dq_answer_key_test") == 67
    assert q("SELECT SUM(matched) FROM analytics.dq_answer_key_test") == 67
    assert q("SELECT SUM(missed) + SUM(extra) FROM analytics.dq_answer_key_test") == 0


def test_pep_exception_is_a_warning_not_an_error(q):
    assert q("SELECT severity FROM analytics.dq_check_catalogue "
             "WHERE check_name = 'pep_rating_policy_exception'") == "warning"


# ---------------------------------------------------------------- rules (independent recomputation)
def _cases_with_activity(raw):
    c = raw("customers").drop_duplicates("customer_id")
    a = raw("monthly_activity")
    k = raw("review_cases")
    return (k.merge(a, left_on=["customer_id", "review_month"], right_on=["customer_id", "month"])
             .merge(c, on="customer_id"))


@pytest.mark.parametrize("rule,condition", [
    ("R06", lambda m: m.txn_value > 2 * m.expected_monthly_turnover),
    ("R07", lambda m: m.cash_share > 0.30),
    ("R08", lambda m: m.high_risk_country_share > 0.10),
    ("R09", lambda m: m.tm_alerts >= 2),
])
def test_activity_rules_match_pandas_recomputation(q, raw, rule, condition):
    m = _cases_with_activity(raw)
    expected = int(condition(m).sum())
    got = q("SELECT COUNT(*) FROM analytics.rule_hits WHERE eval_type = 'CASE' AND rule_code = %s", (rule,))
    assert got == expected


def test_overdue_rule_matches_pandas_recomputation(q, raw):
    c = raw("customers").drop_duplicates("customer_id")
    expected = int((pd.to_datetime(c.next_review_due) < AS_OF).sum())
    assert q("SELECT COUNT(*) FROM analytics.rule_hits WHERE rule_code = 'R12'") == expected


def test_country_status_is_point_in_time_nigeria(q):
    # Nigeria removed from the FATF list on 24 Oct 2025
    sql = """SELECT COUNT(*) FROM analytics.rule_hits h
             JOIN analytics.rule_evaluations e USING (eval_id)
             JOIN staging.customers c ON c.customer_id = e.customer_id
             WHERE h.rule_code = 'R01' AND c.country_code = 'NG' AND e.eval_date {} DATE '2025-10-24'"""
    assert q(sql.format("<")) > 0
    assert q(sql.format(">=")) == 0


def test_country_status_is_point_in_time_virgin_islands(q):
    # Virgin Islands (UK) added on 13 Jun 2025
    sql = """SELECT COUNT(*) FROM analytics.rule_hits h
             JOIN analytics.rule_evaluations e USING (eval_id)
             JOIN staging.customers c ON c.customer_id = e.customer_id
             WHERE h.rule_code = 'R01' AND c.country_code = 'VG' AND e.eval_date < DATE '2025-06-13'"""
    assert q(sql) == 0


def test_every_sanctions_hit_is_priority_one(q):
    assert q("SELECT COUNT(*) FROM analytics.review_queue WHERE reason_codes LIKE '%R04%' "
             "AND priority <> 'P1'") == 0


def test_queue_has_one_row_per_flagged_customer(q):
    assert q("SELECT COUNT(*) FROM analytics.review_queue") == \
           q("SELECT COUNT(DISTINCT customer_id) FROM analytics.rule_hits WHERE eval_type = 'PORTFOLIO'")


# ---------------------------------------------------------------- model: leakage and protocol
def test_periods_do_not_overlap(q):
    rows = []
    for p in ["train", "validation", "test", "monitoring"]:
        rows.append((q("SELECT MIN(review_date) FROM analytics.model_features WHERE period = %s", (p,)),
                     q("SELECT MAX(review_date) FROM analytics.model_features WHERE period = %s", (p,))))
    for (_, end), (start, _) in zip(rows, rows[1:]):
        assert end < start


def test_activity_features_come_from_the_month_before_the_review(q):
    # last_cash_share must equal the previous month's cash share, never the review month's
    mismatches = q("""
        SELECT COUNT(*) FROM analytics.model_features f
        JOIN staging.monthly_activity a
          ON a.customer_id = f.customer_id
         AND a.month_start = (f.review_month - INTERVAL '1 month')::date
        WHERE f.last_cash_share IS DISTINCT FROM a.cash_share AND a.txn_value >= 0""")
    assert mismatches == 0


def test_missing_history_stays_missing_regression_d1(q):
    # Defect D1: GREATEST() ignored NULL, so missing history became ln(0.05)
    assert q("SELECT COUNT(*) FROM analytics.model_features "
             "WHERE months_of_history = 0 AND log_last_turnover_ratio IS NOT NULL") == 0
    assert q("SELECT COUNT(*) FROM analytics.model_features "
             "WHERE log_last_turnover_ratio <= ln(0.05) + 1e-9") == 0


def test_every_case_is_scored_once(q):
    assert q("SELECT COUNT(*) FROM analytics.model_scores") == \
           q("SELECT COUNT(*) FROM analytics.model_features") == 6374


def test_threshold_matches_rules_recall_on_validation(q):
    model_recall = q("SELECT SUM(flagged * analyst_escalated)::numeric / SUM(analyst_escalated) "
                     "FROM analytics.model_scores WHERE period = 'validation'")
    rules_recall = q("SELECT SUM(any_rule * analyst_escalated)::numeric / SUM(analyst_escalated) "
                     "FROM analytics.model_scores WHERE period = 'validation'")
    model_load = q("SELECT AVG(flagged) FROM analytics.model_scores WHERE period = 'validation'")
    rules_load = q("SELECT AVG(any_rule) FROM analytics.model_scores WHERE period = 'validation'")
    assert model_recall >= rules_recall
    assert model_load < rules_load


# ---------------------------------------------------------------- monitoring
def test_no_drift_before_deployment(q):
    assert q("SELECT COUNT(*) FROM analytics.monitoring_status "
             "WHERE month < DATE '2025-10-01' AND status NOT LIKE 'STABLE%%'") == 0


def test_december_drift_breach_but_no_retraining_trigger(q):
    assert q("SELECT status FROM analytics.monitoring_status WHERE month = DATE '2025-12-01'") == "ACT"
    assert q("SELECT COUNT(*) FROM analytics.monitoring_status WHERE status = 'RETRAIN REVIEW'") == 0
