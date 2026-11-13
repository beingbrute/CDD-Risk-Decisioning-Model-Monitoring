"""
Monitor the escalation model after deployment: input drift, score drift and performance.

Baseline = the training window (January-June 2025). Each month from July to December is
compared with it. July-September are pre-deployment reference months (validation and test);
October-December are the simulated post-deployment period.

Drift: Population Stability Index (PSI), 10 bins cut at the baseline deciles.
    PSI < 0.10        stable
    0.10 - 0.25       investigate
    > 0.25            act
  Measured on (a) monthly portfolio activity of all active customers, (b) the model's input
  features on review cases, and (c) the model score itself.

Performance: monthly AUC, and precision / recall / workload at the threshold chosen on
validation. A month is a performance concern if AUC is more than AUC_DROP below the
validation AUC.

Breach in a month: any PSI above 0.25 (portfolio activity, model input or score), or AUC
more than AUC_DROP below validation.
Retraining review (decided before looking at the monitoring months): a breach in TWO
CONSECUTIVE post-deployment months.

Outputs:
  analytics.monitoring_psi, analytics.monitoring_performance, analytics.monitoring_status
  reports/monitoring.md, reports/monitoring_psi.png, reports/monitoring_performance.png

Usage:
    python src\\monitor.py --port 5433
"""
import argparse
import getpass
import io
import json
import os
import sys
import warnings
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import psycopg2
from sklearn.metrics import roc_auc_score

warnings.filterwarnings("ignore", message=".*pandas only supports SQLAlchemy.*")

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
PSI_INVESTIGATE, PSI_ACT = 0.10, 0.25
AUC_DROP = 0.05
BASELINE_END = "2025-06-30"
DEPLOYED_FROM = pd.Timestamp("2025-10-01")

PORTFOLIO_VARS = ["turnover_ratio", "cash_share", "cross_border_share", "high_risk_country_share"]
MODEL_INPUT_VARS = ["log_last_turnover_ratio", "last_cash_share", "last_cross_border_share",
                    "last_hrtc_flow_share", "alerts_4m"]


def connect(args):
    password = os.environ.get("PGPASSWORD") or getpass.getpass(f"PostgreSQL password for {args.user}: ")
    try:
        return psycopg2.connect(host=args.host, port=args.port, user=args.user, password=password, dbname=args.db)
    except psycopg2.OperationalError as e:
        print(f"ERROR: could not connect to PostgreSQL: {e}")
        sys.exit(2)


def psi(base, cur, bins=10):
    base, cur = np.asarray(base, float), np.asarray(cur, float)
    base, cur = base[~np.isnan(base)], cur[~np.isnan(cur)]
    edges = np.unique(np.quantile(base, np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:                       # almost constant variable
        return 0.0
    edges[0], edges[-1] = -np.inf, np.inf
    pb = np.histogram(base, edges)[0] / len(base)
    pc = np.histogram(cur, edges)[0] / len(cur)
    pb, pc = np.clip(pb, 1e-4, None), np.clip(pc, 1e-4, None)
    return float(np.sum((pc - pb) * np.log(pc / pb)))


def psi_status(v):
    return "act" if v > PSI_ACT else "investigate" if v > PSI_INVESTIGATE else "stable"


def write_table(con, name, df, types):
    with con, con.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {name}")
        cur.execute(f"CREATE TABLE {name} ({', '.join(f'{c} {t}' for c, t in types.items())})")
        buf = io.StringIO()
        df[list(types)].to_csv(buf, index=False, header=False)
        buf.seek(0)
        cur.copy_expert(f"COPY {name} FROM STDIN WITH (FORMAT csv)", buf)


def md_table(df):
    cols = list(df.columns)
    out = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for _, r in df.astype(object).iterrows():
        out.append("| " + " | ".join(str(r[c]) for c in cols) + " |")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", default=os.environ.get("PGPORT", "5432"))
    ap.add_argument("--user", default="postgres")
    ap.add_argument("--db", default="cdd")
    args = ap.parse_args()

    meta_path = ROOT / "models" / "champion_metadata.json"
    if not meta_path.exists():
        print("ERROR: models/champion_metadata.json not found. Run src/train_model.py first.")
        sys.exit(2)
    meta = json.loads(meta_path.read_text())
    thr, val_auc = meta["threshold"], meta["validation_auc"]

    con = connect(args)
    act = pd.read_sql("""
        SELECT a.month_start, a.txn_value / NULLIF(c.expected_monthly_turnover, 0) AS turnover_ratio,
               a.cash_share, a.cross_border_share, a.high_risk_country_share
        FROM staging.monthly_activity a JOIN staging.customers c USING (customer_id)
        WHERE a.txn_value >= 0""", con, parse_dates=["month_start"])
    cases = pd.read_sql("""
        SELECT s.case_id, s.review_date, s.period, s.score, s.flagged, s.analyst_escalated,
               f.log_last_turnover_ratio, f.last_cash_share, f.last_cross_border_share,
               f.last_hrtc_flow_share, f.alerts_4m
        FROM analytics.model_scores s JOIN analytics.model_features f USING (case_id)""",
                        con, parse_dates=["review_date"])
    cases["month"] = cases.review_date.dt.to_period("M").dt.to_timestamp()

    # ---------- drift ----------
    rows = []
    base_act = act[act.month_start <= BASELINE_END]
    base_cases = cases[cases.review_date <= BASELINE_END]
    months = sorted(cases.month[cases.review_date > BASELINE_END].unique())
    for m in months:
        cur_act = act[act.month_start == m]
        cur_cases = cases[cases.month == m]
        for v in PORTFOLIO_VARS:
            rows.append(("portfolio activity", v, m, psi(base_act[v], cur_act[v])))
        for v in MODEL_INPUT_VARS:
            rows.append(("model input", v, m, psi(base_cases[v], cur_cases[v])))
        rows.append(("model score", "score", m, psi(base_cases.score, cur_cases.score)))
    drift = pd.DataFrame(rows, columns=["source", "variable", "month", "psi"])
    drift["psi"] = drift.psi.round(3)
    drift["status"] = drift.psi.map(psi_status)

    # ---------- performance ----------
    perf_rows = []
    for m in months:
        g = cases[cases.month == m]
        y, s, f = g.analyst_escalated.to_numpy(), g.score.to_numpy(), g.flagged.to_numpy()
        tp = int((f & y).sum())
        auc = roc_auc_score(y, s)
        perf_rows.append({"month": m, "period": g.period.iloc[0], "cases": len(g), "escalated": int(y.sum()),
                          "auc": round(auc, 3), "auc_vs_validation": round(auc - val_auc, 3),
                          "workload": round(f.mean(), 3), "precision": round(tp / max(f.sum(), 1), 3),
                          "recall": round(tp / max(y.sum(), 1), 3)})
    perf = pd.DataFrame(perf_rows)

    # ---------- monthly status and retraining trigger ----------
    worst_input = drift[drift.source == "model input"].groupby("month").psi.max()
    score_psi = drift[drift.source == "model score"].set_index("month").psi
    worst_portfolio = drift[drift.source == "portfolio activity"].groupby("month").psi.max()
    st = perf[["month", "period", "auc", "auc_vs_validation"]].copy()
    st["max_portfolio_psi"] = st.month.map(worst_portfolio)
    st["max_model_input_psi"] = st.month.map(worst_input)
    st["score_psi"] = st.month.map(score_psi)
    st["breach"] = ((st[["max_portfolio_psi", "max_model_input_psi", "score_psi"]].max(axis=1) > PSI_ACT)
                    | (st.auc_vs_validation < -AUC_DROP))
    st["watch"] = ((st[["max_portfolio_psi", "max_model_input_psi", "score_psi"]].max(axis=1) > PSI_INVESTIGATE)
                   | (st.auc_vs_validation < -AUC_DROP))
    deployed = st.month >= DEPLOYED_FROM
    consec = st.breach & st.breach.shift(1, fill_value=False) & deployed & deployed.shift(1, fill_value=False)
    st["status"] = np.where(consec, "RETRAIN REVIEW",
                   np.where(st.breach, "ACT", np.where(st.watch, "INVESTIGATE", "STABLE")))
    st.loc[~deployed, "status"] = st.loc[~deployed, "status"] + " (pre-deployment)"

    # ---------- save ----------
    for d in (drift, perf, st):
        d["month"] = pd.to_datetime(d.month).dt.date
    write_table(con, "analytics.monitoring_psi", drift,
                {"source": "text", "variable": "text", "month": "date", "psi": "numeric", "status": "text"})
    write_table(con, "analytics.monitoring_performance", perf,
                {"month": "date", "period": "text", "cases": "int", "escalated": "int", "auc": "numeric",
                 "auc_vs_validation": "numeric", "workload": "numeric", "precision": "numeric", "recall": "numeric"})
    write_table(con, "analytics.monitoring_status", st,
                {"month": "date", "period": "text", "auc": "numeric", "auc_vs_validation": "numeric",
                 "max_portfolio_psi": "numeric", "max_model_input_psi": "numeric", "score_psi": "numeric",
                 "breach": "boolean", "watch": "boolean", "status": "text"})
    con.close()

    REPORTS.mkdir(exist_ok=True)
    # chart 1: PSI
    fig, ax = plt.subplots(figsize=(9, 4.5))
    for (src, var), g in drift.groupby(["source", "variable"]):
        if src == "model input" and g.psi.max() < PSI_INVESTIGATE:
            continue
        ax.plot(pd.to_datetime(g.month), g.psi, marker="o", label=f"{var} ({src})")
    ax.axhline(PSI_INVESTIGATE, ls="--", color="grey", lw=1)
    ax.axhline(PSI_ACT, ls="--", color="black", lw=1)
    ax.axvline(DEPLOYED_FROM, color="grey", lw=1, alpha=.5)
    ax.text(DEPLOYED_FROM, ax.get_ylim()[1] * .95, " deployed", fontsize=8, va="top")
    ax.set_title("Population Stability Index vs January-June baseline")
    ax.set_ylabel("PSI")
    ax.legend(fontsize=7, loc="upper left")
    fig.tight_layout()
    fig.savefig(REPORTS / "monitoring_psi.png", dpi=150)
    plt.close(fig)
    # chart 2: performance
    fig, ax = plt.subplots(figsize=(9, 4))
    ax.plot(pd.to_datetime(perf.month), perf.auc, marker="o", label="monthly AUC")
    ax.axhline(val_auc, color="grey", lw=1, label=f"validation AUC {val_auc:.3f}")
    ax.axhline(val_auc - AUC_DROP, ls="--", color="black", lw=1, label=f"trigger level {val_auc - AUC_DROP:.3f}")
    ax.axvline(DEPLOYED_FROM, color="grey", lw=1, alpha=.5)
    ax.set_ylim(0.5, 1)
    ax.set_title("Model AUC by month")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(REPORTS / "monitoring_performance.png", dpi=150)
    plt.close(fig)

    wide = drift.pivot_table(index=["source", "variable"], columns="month", values="psi").reset_index()
    wide.columns = [str(c) for c in wide.columns]
    retrain = st[st.status == "RETRAIN REVIEW"]
    breaches = st[(st.month >= DEPLOYED_FROM.date()) & st.breach]
    if len(retrain):
        decision = f"The retraining trigger was met in {', '.join(str(m) for m in retrain.month)}."
    elif len(breaches):
        decision = (f"Breach in {', '.join(str(m) for m in breaches.month)}, but not in two consecutive months, "
                    "so the retraining trigger was not met. Action: investigate the drifting segment, confirm "
                    "performance as outcomes arrive, and prepare retraining if the next month also breaches.")
    else:
        decision = "No breach in October-December; the retraining trigger was not met."
    report = f"""# Model monitoring report

Generated by `src/monitor.py`. Baseline: training window January-June 2025.
July-September are pre-deployment reference months; October-December are the simulated
post-deployment period. Figures come from synthetic data.

**Rules (set before looking at the monitoring months):** PSI below {PSI_INVESTIGATE} stable,
{PSI_INVESTIGATE}-{PSI_ACT} investigate, above {PSI_ACT} act. Performance concern: AUC more than {AUC_DROP}
below the validation AUC ({val_auc:.3f}). A month is a breach (ACT) if any PSI is above {PSI_ACT}
or AUC drops by more than {AUC_DROP}. Retraining review: a breach in two consecutive
post-deployment months.

## 1. Monthly status
{md_table(st.drop(columns=["breach", "watch"]))}

**Decision:** {decision}

## 2. PSI by variable and month
{md_table(wide)}

![PSI](monitoring_psi.png)

## 3. Performance by month (threshold {thr:.4f} from validation)
{md_table(perf)}

![AUC](monitoring_performance.png)

Monthly samples are small (about 500-600 cases and 80-100 escalations), so month-to-month
AUC moves by a few points without any real change.

## 4. How to read this
- Portfolio activity PSI shows a change in customer behaviour as soon as it happens.
- Model inputs use the previous completed month, so the same change reaches the model's
  inputs one month later.
- A drift alert means "the population has changed; check whether the model still works".
  It is not proof that the model is wrong. The performance section answers that question,
  but only once analyst outcomes are known, which in practice lags the scores.
"""
    (REPORTS / "monitoring.md").write_text(report, encoding="utf-8")

    print("\nMonthly status")
    print(st.drop(columns=["breach", "watch"]).to_string(index=False))
    print("\nPortfolio activity PSI (cross_border_share):",
          drift[(drift.variable == "cross_border_share")].set_index("month").psi.to_dict())
    print(f"\n{decision}")
    print("Saved: analytics.monitoring_psi / monitoring_performance / monitoring_status, reports/monitoring.md, 2 charts")


if __name__ == "__main__":
    main()
