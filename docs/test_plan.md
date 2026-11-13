# Test plan: CDD Risk Decisioning & Model Monitoring

**Scope:** data generation, PostgreSQL load (RAW → STAGING → ANALYTICS), data-quality checks,
CDD rules and review queue, rule effectiveness, model features, model training and validation,
monitoring, Power BI export and dashboard.

**Environment:** Windows 11, Python 3.13.9 (Anaconda), PostgreSQL 17.12 (port 5433),
pandas, scikit-learn, pytest, Power BI Desktop. CI: GitHub Actions, Ubuntu, PostgreSQL 17, Python 3.12.

**Test data:** synthetic, from `src/generate_data.py` (seed 42). Real reference data: FATF lists
in force during 2025. 67 planted data-quality issues listed in `data/raw/expected_issues.csv`.

**Status key:** Pass · Fail · Pending (not yet run)

---

## 1. Pipeline test cases

| ID | Test | Expected result | Actual result | Status |
|---|---|---|---|---|
| TC01 | Generate data (`generate_data.py`) | 8 files; 5,005 customer rows (5,000 unique); 6,374 review cases; 67 planted issues in 11 checks | As expected; output identical to the reference run line for line (13 Nov 2026) | Pass |
| TC02 | Load RAW (`load_postgres.py`) | Database `cdd`, schemas raw / staging / analytics; 8 tables; file rows = database rows | 8 tables OK; schemas present | Pass |
| TC03 | FATF history in the database | Nigeria listed until the 2025-10 version; Virgin Islands (UK) from 2025-06 | Query result as expected | Pass |
| TC04 | Staging reconciliation (`01_staging.sql`) | RAW rows = STAGING rows for 6 tables; 0 unparsed values | All OK | Pass |
| TC05 | Data-quality checks vs answer key (`02_dq_checks.sql`) | 11 checks; 67 found; 67 matched; 0 missed; 0 extra | ALL MATCH | Pass |
| TC06 | Rules (`03_risk_rules.sql`) | 12 rules evaluated for portfolio and cases; R12 portfolio only | Counts as expected (e.g. R06 1,055 cases, R12 533 customers) | Pass |
| TC07 | Review queue (`04_review_queue.sql`) | 2,447 customers; P1 169, P2 553, P3 1,725; queue position 1 = CUST02340 (P1, 81) | As expected | Pass |
| TC08 | Rule effectiveness (`05_rule_effectiveness.sql`) | Any rule: 3,105 cases, precision 0.239, recall 0.689 | As expected | Pass |
| TC09 | Model features (`06_model_features.sql`) | 3,087 / 1,067 / 547 / 1,673 cases by period; activity only from completed months | As expected | Pass |
| TC10 | Model training (`train_model.py`) | Champion chosen by the pre-set rule; threshold on validation; test scored once | Logistic regression; threshold 0.2070; test AUC 0.762 (0.709-0.812) | Pass |
| TC11 | Monitoring (`monitor.py`) | Jul-Sep stable; drift detected from Oct; status per month; decision rule applied | Jul-Sep STABLE; Oct-Nov INVESTIGATE; Dec ACT; trigger not met | Pass |
| TC12 | Power BI export (`export_powerbi.py`) | 12 CSV files | 12 files written | Pass |
| TC13 | Dashboard page 1: data quality | Cards 11 / 67 / 67 / 0; clicking a bar filters the exception table | As expected | Pass |
| TC14 | Dashboard page 2: review queue | Cards 2,447 / 169 / 533; table starts at CUST02340 | As expected; P1 shown in red (13 Nov 2026) | Pass |
| TC15 | Dashboard page 3: FATF matrix | 4 list versions; Nigeria not listed in 2025-10; Iran, Myanmar, DPRK call for action | As expected after D2 fix | Pass |
| TC16 | Dashboard page 4: model and monitoring | Drift crosses 0.1 in Oct and 0.25 in Dec; AUC above 0.735 every month | Portfolio PSI 0.11 (Oct), 0.29 (Dec); lowest AUC 0.738 (Nov) | Pass |
| TC17 | Automated tests (`pytest`) | 40 passed | 40 passed in 5.59 s (13 Nov 2026, 14:58) | Pass |
| TC18 | CI on GitHub (`.github/workflows/ci.yml`) | Full pipeline rebuilt from scratch; all tests pass | | Pending |

## 2. Automated tests (`tests/`)

| File | What it checks |
|---|---|
| `test_reference_data.py` (6 tests) | FATF versions and dates; call-for-action list; increased-monitoring list after each published change; UK high-risk flag; rules table complete, with illustrative thresholds labelled; 67 planted issues |
| `test_metrics.py` (9 tests) | PSI is 0 for identical data, high for a shifted one, ignores missing values; PSI bands; threshold gives target recall with the least workload |
| `test_pipeline_db.py` (25 tests) | Row counts file → RAW → STAGING; invalid ratings not auto-corrected; data-quality checks match the answer key; activity rules R06-R09 and overdue rule R12 match an independent pandas recomputation; FATF status point-in-time (Nigeria, Virgin Islands (UK)); every sanctions hit is P1; periods do not overlap; activity features come from the month before the review; regression test for D1; every case scored once; threshold matches rules recall with less workload; no drift before deployment; December breach without retraining trigger |

The database tests are skipped when `PGPASSWORD` is not set, so the other 15 always run.

## 3. Defects found

| ID | Found by | Description | Fix | Retest |
|---|---|---|---|---|
| D1 | Monitoring (TC11): PSI 0.35 on `log_last_turnover_ratio` in July, before any drift existed | `ln(GREATEST(ratio, 0.05))`: PostgreSQL's GREATEST ignores NULL, so cases with no earlier activity (all January cases, new customers) were given "very low turnover" instead of "unknown". The model was trained on this wrong value | Keep missing history NULL (`CASE WHEN ratio IS NOT NULL ...`); retrained | Jul-Sep input PSI ≤ 0.048; validation AUC 0.780 → 0.785; test AUC 0.756 → 0.762; regression test added |
| D2 | Dashboard check against FATF lists (TC15) | Power BI read `list_version` values such as "2024-10" as dates and grouped them by year, so the matrix showed 2 columns and wrong statuses (e.g. Nigeria listed in late 2025) | Set `list_version` to Text in Power Query | Matrix shows 4 versions; all transitions correct |
| D3 | Building relationships | `queue_reasons` loaded without column names (Column1, Column2 …) because every column is text | Power Query: Use First Row as Headers | Columns named; relationships created |
| D4 | Model view review | Power BI auto-created relationships on unrelated columns (monitoring tables joined on `auc`; rules tables on `description`) | Deleted; created 5 relationships on keys (`customer_id`, `rule_code`, `check_name`) | Filters work as intended (TC13) |

## 4. Known limitations
- Customers, activity and escalation outcomes are synthetic; results show the method, not real-world accuracy.
- Documents and shareholder registers exist only as a 31 Dec 2025 snapshot.
- No sanctions-match resolution field: every screening hit is treated as unresolved.
- Monthly activity is aggregated, so the model uses completed months only (one month behind the rules).
