"""The Power BI export: every file the dashboard reads exists and has the columns it uses."""
from pathlib import Path

import pandas as pd
import pytest

OUT = Path(__file__).resolve().parents[1] / "data" / "powerbi"

EXPECTED = {
    "dq_summary": ["check_name", "severity", "records_failed", "status"],
    "dq_answer_key_test": ["check_name", "expected", "matched", "missed", "extra"],
    "dq_exceptions": ["check_name", "record_key", "detail"],
    "review_queue": ["queue_position", "customer_id", "priority", "priority_score", "reason_codes",
                     "next_actions", "days_overdue"],
    "queue_reasons": ["customer_id", "rule_code"],
    "cdd_rules": ["rule_code", "description", "basis_type", "weight"],
    "rule_effectiveness": ["rule_code", "precision", "recall", "lift"],
    "fatf_status_history": ["country_name", "list_version", "fatf_status"],
    "model_vs_rules": ["period", "approach", "workload", "precision", "recall", "period_start"],
    "monitoring_status": ["month", "status", "max_portfolio_psi", "score_psi", "auc"],
    "monitoring_psi": ["source", "variable", "month", "psi"],
    "monitoring_performance": ["month", "auc"],
}


@pytest.mark.parametrize("name,columns", EXPECTED.items())
def test_export_file_has_dashboard_columns(name, columns):
    path = OUT / f"{name}.csv"
    if not path.exists():
        pytest.skip("run src/export_powerbi.py first")
    df = pd.read_csv(path)
    assert len(df) > 0
    assert set(columns) <= set(df.columns)


def test_dq_export_matches_answer_key():
    path = OUT / "dq_answer_key_test.csv"
    if not path.exists():
        pytest.skip("run src/export_powerbi.py first")
    df = pd.read_csv(path)
    assert df.matched.sum() == 67 and (df.missed.sum() + df.extra.sum()) == 0
