"""
Train and validate the escalation model.

Reads analytics.model_features (built by sql/06_model_features.sql) and follows a
fixed protocol decided before training:

  train       Jan-Jun 2025   fit candidate models
  validation  Jul-Aug 2025   pick the champion and the decision threshold
  test        Sep 2025       score once with the chosen model and threshold
  monitoring  Oct-Dec 2025   scored only; analysed in monitor.py

Candidates: logistic regression (explainable) and random forest (challenger).
Selection rule (fixed in advance): the random forest replaces logistic regression only
if its validation PR-AUC is higher by at least MIN_GAIN_FOR_COMPLEX_MODEL.
Threshold rule: the lowest workload whose validation recall is at least the recall of
the current rules process ("any rule fires") on the same validation cases.

Outputs:
  models/champion.joblib, models/champion_metadata.json
  analytics.model_scores                  (score for every case, all periods)
  reports/model_validation.md             (validation report)
  reports/model_*.csv                     (tables behind the report)

Usage:
    python src\\train_model.py --port 5433
"""
import argparse
import getpass
import io
import json
import os
import sys
import warnings
from datetime import datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import psycopg2
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

warnings.filterwarnings("ignore", message=".*pandas only supports SQLAlchemy.*")

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
MODELS = ROOT / "models"
SEED = 42
MIN_GAIN_FOR_COMPLEX_MODEL = 0.02
N_BOOT = 2000

FEATURES = [
    # customer profile
    "is_company", "channel_online", "channel_intermediary", "industry_risk", "cash_intensive",
    "tenure_years", "bank_risk_rating", "domestic_pep", "non_domestic_pep", "trigger_review",
    # rules known on the review date
    "r01_hrtc_customer", "r02_hrtc_owner", "r03_pep", "r04_sanctions_hit",
    "r05_adverse_media", "r10_document_gap", "r11_ownership_gap",
    # activity from completed months before the review
    "log_last_turnover_ratio", "log_prior3_turnover_ratio", "last_cash_share", "max_cash_share_4m",
    "last_cross_border_share", "last_hrtc_flow_share", "last_tm_alerts", "alerts_4m",
    "months_of_history",
]


# ------------------------------------------------------------------ helpers
def connect(args):
    password = os.environ.get("PGPASSWORD") or getpass.getpass(f"PostgreSQL password for {args.user}: ")
    try:
        return psycopg2.connect(host=args.host, port=args.port, user=args.user, password=password, dbname=args.db)
    except psycopg2.OperationalError as e:
        print(f"ERROR: could not connect to PostgreSQL: {e}")
        sys.exit(2)


def candidates():
    def lr(c):
        return make_pipeline(SimpleImputer(strategy="median", add_indicator=True), StandardScaler(),
                             LogisticRegression(C=c, max_iter=5000))

    def rf(depth):
        return make_pipeline(SimpleImputer(strategy="median", add_indicator=True),
                             RandomForestClassifier(n_estimators=400, max_depth=depth, min_samples_leaf=20,
                                                    random_state=SEED, n_jobs=-1))
    return {
        "logreg_C0.05": ("logistic_regression", lr(0.05)),
        "logreg_C0.3": ("logistic_regression", lr(0.3)),
        "logreg_C1.0": ("logistic_regression", lr(1.0)),
        "rf_depth6": ("random_forest", rf(6)),
        "rf_depthNone": ("random_forest", rf(None)),
    }


def at_threshold(y, score, thr):
    flag = score >= thr
    tp = int((flag & (y == 1)).sum())
    return {"workload": round(float(flag.mean()), 3), "cases_flagged": int(flag.sum()),
            "precision": round(float(tp / max(flag.sum(), 1)), 3),
            "recall": round(float(tp / max((y == 1).sum(), 1)), 3)}


def threshold_for_recall(y, score, target):
    """Highest threshold (= lowest workload) whose recall is at least target."""
    order = np.argsort(-score)
    hits = np.cumsum(y[order] == 1) / max((y == 1).sum(), 1)
    k = int(np.argmax(hits >= target))
    return float(score[order][k])


def bootstrap_ci(y, score, metric, n=N_BOOT, seed=SEED):
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(n):
        i = rng.integers(0, len(y), len(y))
        if y[i].min() == y[i].max():
            continue
        vals.append(metric(y[i], score[i]))
    return np.percentile(vals, [2.5, 97.5])


def md_table(df):
    cols = list(df.columns)
    out = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for _, r in df.astype(object).iterrows():
        out.append("| " + " | ".join(str(r[c]) for c in cols) + " |")
    return "\n".join(out)


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", default=os.environ.get("PGPORT", "5432"))
    ap.add_argument("--user", default="postgres")
    ap.add_argument("--db", default="cdd")
    args = ap.parse_args()
    con = connect(args)

    df = pd.read_sql("SELECT * FROM analytics.model_features ORDER BY review_date, case_id", con)
    rules = pd.read_sql("""
        SELECT h.eval_id AS case_id, SUM(w.weight) AS rule_points, 1 AS any_rule
        FROM analytics.rule_hits h JOIN analytics.rule_weights w USING (rule_code)
        WHERE h.eval_type = 'CASE' GROUP BY h.eval_id""", con)
    df = df.merge(rules, on="case_id", how="left").fillna({"rule_points": 0, "any_rule": 0})
    df["any_rule"] = df.any_rule.astype(int)
    missing = [f for f in FEATURES if f not in df.columns]
    if missing:
        print(f"ERROR: features missing from analytics.model_features: {missing}")
        sys.exit(2)

    part = {p: df[df.period == p] for p in ["train", "validation", "test", "monitoring"]}
    X = {p: d[FEATURES] for p, d in part.items()}
    y = {p: d.analyst_escalated.to_numpy() for p, d in part.items()}
    print("Cases per period:", {p: len(d) for p, d in part.items()})

    # ---------- 1. fit candidates on train, compare on validation ----------
    rows, fitted = [], {}
    for name, (family, model) in candidates().items():
        model.fit(X["train"], y["train"])
        s = model.predict_proba(X["validation"])[:, 1]
        fitted[name] = (family, model)
        rows.append({"candidate": name, "family": family,
                     "val_auc": round(roc_auc_score(y["validation"], s), 3),
                     "val_pr_auc": round(average_precision_score(y["validation"], s), 3),
                     "val_brier": round(brier_score_loss(y["validation"], s), 4)})
    cand = pd.DataFrame(rows)
    for base, col in [("rules process (rule points)", "rule_points"), ("bank risk rating", "bank_risk_rating")]:
        s = part["validation"][col].fillna(1).to_numpy()
        cand.loc[len(cand)] = {"candidate": base, "family": "baseline",
                               "val_auc": round(roc_auc_score(y["validation"], s), 3),
                               "val_pr_auc": round(average_precision_score(y["validation"], s), 3),
                               "val_brier": ""}
    print("\nValidation results (Jul-Aug):")
    print(cand.to_string(index=False))

    best_lr = cand[cand.family == "logistic_regression"].sort_values("val_pr_auc", ascending=False).iloc[0]
    best_rf = cand[cand.family == "random_forest"].sort_values("val_pr_auc", ascending=False).iloc[0]
    gain = best_rf.val_pr_auc - best_lr.val_pr_auc
    champion = best_rf.candidate if gain >= MIN_GAIN_FOR_COMPLEX_MODEL else best_lr.candidate
    challenger = best_lr.candidate if champion == best_rf.candidate else best_rf.candidate
    reason = (f"Random forest PR-AUC is {gain:+.3f} vs logistic regression; the rule requires at least "
              f"+{MIN_GAIN_FOR_COMPLEX_MODEL:.2f} to accept the less explainable model.")
    model = fitted[champion][1]
    print(f"\nChampion: {champion}. {reason}")

    # ---------- 2. threshold on validation ----------
    s_val = model.predict_proba(X["validation"])[:, 1]
    rules_val = at_threshold(y["validation"], part["validation"].any_rule.to_numpy(), 1)
    thr = threshold_for_recall(y["validation"], s_val, rules_val["recall"])
    model_val = at_threshold(y["validation"], s_val, thr)
    print(f"Threshold {thr:.4f}: validation {model_val} vs rules {rules_val}")

    # ---------- 3. calibration on validation ----------
    cal = pd.DataFrame({"score": s_val, "y": y["validation"]})
    cal["band"] = pd.qcut(cal.score, 10, labels=False, duplicates="drop") + 1
    cal = cal.groupby("band").agg(cases=("y", "size"), mean_predicted=("score", "mean"),
                                  observed_rate=("y", "mean")).round(3).reset_index()
    cal["band"] = cal.band.astype(int)

    # ---------- 4. segments on validation ----------
    seg_rows = []
    pv = part["validation"].assign(score=s_val)
    for col, label in [("is_company", {0: "Individual", 1: "Company"}), ("channel", None)]:
        for val, g in pv.groupby(col):
            yy, ss = g.analyst_escalated.to_numpy(), g.score.to_numpy()
            seg_rows.append({"segment": f"{col}={label[val] if label else val}", "cases": len(g),
                             "escalation_rate": round(float(yy.mean()), 3),
                             "auc": round(float(roc_auc_score(yy, ss)), 3) if 0 < yy.sum() < len(yy) else "n/a",
                             **{k: v for k, v in at_threshold(yy, ss, thr).items() if k in ("workload", "recall", "precision")}})
    seg = pd.DataFrame(seg_rows)

    # ---------- 5. explainability ----------
    if fitted[champion][0] == "logistic_regression":
        lr_step = model.named_steps["logisticregression"]
        names = model.named_steps["simpleimputer"].get_feature_names_out(FEATURES)
        expl = (pd.DataFrame({"feature": names, "standardised_coefficient": lr_step.coef_[0].round(3)})
                .assign(abs=lambda d: d.standardised_coefficient.abs())
                .sort_values("abs", ascending=False).drop(columns="abs"))
    else:
        pi = permutation_importance(model, X["validation"], y["validation"], scoring="average_precision",
                                    n_repeats=10, random_state=SEED)
        expl = pd.DataFrame({"feature": FEATURES, "pr_auc_drop": pi.importances_mean.round(4)}) \
            .sort_values("pr_auc_drop", ascending=False)

    # ---------- 6. the untouched test, used once ----------
    s_test = model.predict_proba(X["test"])[:, 1]
    yt = y["test"]
    test = {
        "auc": roc_auc_score(yt, s_test), "auc_ci": bootstrap_ci(yt, s_test, roc_auc_score),
        "pr_auc": average_precision_score(yt, s_test), "pr_auc_ci": bootstrap_ci(yt, s_test, average_precision_score),
        "brier": brier_score_loss(yt, s_test),
        "model": at_threshold(yt, s_test, thr),
        "rules": at_threshold(yt, part["test"].any_rule.to_numpy(), 1),
        "rules_auc": roc_auc_score(yt, part["test"].rule_points.to_numpy()),
        "rating_auc": roc_auc_score(yt, part["test"].bank_risk_rating.fillna(1).to_numpy()),
    }
    print(f"\nTest (Sep): AUC {test['auc']:.3f} [{test['auc_ci'][0]:.3f}-{test['auc_ci'][1]:.3f}], "
          f"PR-AUC {test['pr_auc']:.3f} [{test['pr_auc_ci'][0]:.3f}-{test['pr_auc_ci'][1]:.3f}]")
    print(f"  model at threshold: {test['model']}")
    print(f"  rules process:      {test['rules']}")

    # ---------- 7. score every case and save ----------
    df["score"] = model.predict_proba(df[FEATURES])[:, 1]
    df["flagged"] = (df.score >= thr).astype(int)
    out = df[["case_id", "customer_id", "review_date", "period", "score", "flagged", "analyst_escalated", "any_rule"]] \
        .assign(model_name=champion)
    with con, con.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS analytics.model_scores")
        cur.execute("""CREATE TABLE analytics.model_scores (case_id text PRIMARY KEY, customer_id text,
                       review_date date, period text, score numeric, flagged int, analyst_escalated int,
                       any_rule int, model_name text)""")
        buf = io.StringIO()
        out.to_csv(buf, index=False, header=False)
        buf.seek(0)
        cur.copy_expert("COPY analytics.model_scores FROM STDIN WITH (FORMAT csv)", buf)
    con.close()

    MODELS.mkdir(exist_ok=True)
    REPORTS.mkdir(exist_ok=True)
    joblib.dump(model, MODELS / "champion.joblib")
    meta = {"champion": champion, "challenger": challenger, "features": FEATURES, "threshold": thr,
            "train_period": "2025-01-01 to 2025-06-30", "validation_period": "2025-07-01 to 2025-08-31",
            "test_period": "2025-09-01 to 2025-09-30", "selection_rule": reason,
            "validation_auc": float(cand.set_index("candidate").loc[champion, "val_auc"]),
            "validation_pr_auc": float(cand.set_index("candidate").loc[champion, "val_pr_auc"]),
            "trained_at": datetime.now().isoformat(timespec="seconds")}
    (MODELS / "champion_metadata.json").write_text(json.dumps(meta, indent=2))
    cand.to_csv(REPORTS / "model_candidates.csv", index=False)
    cal.to_csv(REPORTS / "model_calibration.csv", index=False)
    seg.to_csv(REPORTS / "model_segments.csv", index=False)
    expl.to_csv(REPORTS / "model_explainability.csv", index=False)

    # ---------- 8. validation report ----------
    m, r = test["model"], test["rules"]
    fewer = 1 - m["cases_flagged"] / max(r["cases_flagged"], 1)
    report = f"""# Model validation report: CDD escalation model

Generated by `src/train_model.py` on {meta['trained_at']}. All figures below are produced by
the script from synthetic data; they show the method, not real-world performance.

## 1. Purpose and outcome
The model ranks CDD review cases by the probability that an analyst escalates the case.
It supports prioritisation; it does not replace the statutory rules (R01, R03, R04, R11),
which still apply to every customer.

The outcome (`analyst_escalated`) was simulated separately from the rules, including a
factor that no rule or model sees, so the model cannot score perfectly by re-learning a rule.

## 2. Data and periods (fixed before training)
| Period | Dates | Cases | Escalated | Use |
|---|---|---|---|---|
| Train | Jan-Jun 2025 | {len(part['train'])} | {int(y['train'].sum())} | Fit candidates |
| Validation | Jul-Aug 2025 | {len(part['validation'])} | {int(y['validation'].sum())} | Choose model and threshold |
| Test | Sep 2025 | {len(part['test'])} | {int(yt.sum())} | Final test, used once |
| Monitoring | Oct-Dec 2025 | {len(part['monitoring'])} | {int(y['monitoring'].sum())} | Post-deployment monitoring (monitor.py) |

**Leakage controls:** activity features use only completed months before the review month;
country status is taken on the review date; activity rules R06-R09 (evaluated on the review
month) are not used as features. January cases have no earlier activity (the data starts
in January); missing activity is imputed with an indicator flag.

## 3. Candidates on validation
{md_table(cand)}

**Champion: `{champion}`** (challenger: `{challenger}`). {reason}

## 4. Decision threshold
Chosen on validation as the lowest workload whose recall is at least that of the current
rules process (any rule fires) on the same cases.

| On validation | Cases flagged | Workload | Precision | Recall |
|---|---|---|---|---|
| Rules process | {rules_val['cases_flagged']} | {rules_val['workload']} | {rules_val['precision']} | {rules_val['recall']} |
| Model (threshold {thr:.4f}) | {model_val['cases_flagged']} | {model_val['workload']} | {model_val['precision']} | {model_val['recall']} |

## 5. Calibration (validation, score deciles)
{md_table(cal)}

Brier score on validation: {cand.set_index('candidate').loc[champion, 'val_brier']}.

## 6. Performance by segment (validation)
{md_table(seg)}

Segments are small; differences should be read with that in mind.

## 7. Final test (September 2025, used once)
| Measure | Model | Rules process | Bank risk rating |
|---|---|---|---|
| AUC (95% bootstrap CI) | {test['auc']:.3f} ({test['auc_ci'][0]:.3f}-{test['auc_ci'][1]:.3f}) | {test['rules_auc']:.3f} (rule points) | {test['rating_auc']:.3f} |
| PR-AUC (95% bootstrap CI) | {test['pr_auc']:.3f} ({test['pr_auc_ci'][0]:.3f}-{test['pr_auc_ci'][1]:.3f}) | | |
| Cases flagged | {m['cases_flagged']} of {len(yt)} | {r['cases_flagged']} of {len(yt)} | |
| Precision | {m['precision']} | {r['precision']} | |
| Recall | {m['recall']} | {r['recall']} | |

At the validation threshold, the model flagged {m['cases_flagged']} cases against {r['cases_flagged']} for the
rules process ({fewer:.0%} {'fewer' if fewer >= 0 else 'more'}), with recall {m['recall']} against {r['recall']}.
The test set has {int(yt.sum())} escalations, so the confidence intervals are wide.

## 8. Explainability
Top drivers ({'standardised logistic regression coefficients' if 'standardised_coefficient' in expl.columns else 'permutation importance on validation'}):

{md_table(expl.head(12))}

Coefficients show association, not cause, and each is conditional on all the others. Where
features overlap (for example R01 and the share of flows to listed jurisdictions), one of
them can take a negative sign even though both relate to higher risk on their own.

## 9. Limitations
- Synthetic customers and simulated outcomes; results show the method only.
- Documents and shareholder registers are a 31 Dec 2025 snapshot, not point-in-time.
- The champion was trained on January-June only and was not refitted before the test.
- Post-deployment behaviour (October-December) is assessed separately in the monitoring step.
"""
    (REPORTS / "model_validation.md").write_text(report, encoding="utf-8")
    print(f"\nSaved: models/champion.joblib, analytics.model_scores ({len(out)} rows), reports/model_validation.md")


if __name__ == "__main__":
    main()
