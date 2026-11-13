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
| TC01 | Generate data (`generate_data.py`) | 8 files; 5,005 customer rows (5,000 unique); activity Oct 2024-Dec 2025; 6,222 review cases; 67 planted issues in 11 checks | As expected; identical to the reference run (13 Nov 2026, after D5 fix) | Pass |
| TC02 | Load RAW (`load_postgres.py`) | Database `cdd`, schemas raw / staging / analytics; 8 tables; file rows = database rows | 8 tables OK; schemas present | Pass |
| TC03 | FATF history in the database | Nigeria listed until the 2025-10 version; Virgin Islands (UK) from 2025-06 | Query result as expected | Pass |
| TC04 | Staging reconciliation (`01_staging.sql`) | RAW rows = STAGING rows for 6 tables; 0 unparsed values | All OK | Pass |
| TC05 | Data-quality checks vs answer key (`02_dq_checks.sql`) | 11 checks; 67 found; 67 matched; 0 missed; 0 extra | ALL MATCH | Pass |
| TC06 | Rules (`03_risk_rules.sql`) | 12 rules for portfolio (31 Dec activity) and cases (last month completed before the review); R12 portfolio only | Counts as expected (e.g. R06 959 cases, R12 531 customers) | Pass |
| TC07 | Review queue (`04_review_queue.sql`) | 2,438 customers; P1 170, P2 554, P3 1,714; queue position 1 = CUST02635 (P1, 95) | As expected | Pass |
| TC08 | Rule effectiveness (`05_rule_effectiveness.sql`) | Any rule: 2,930 cases, precision 0.236, recall 0.673 | As expected | Pass |
| TC09 | Model features (`06_model_features.sql`) | 3,074 / 1,018 / 540 / 1,590 cases by period; activity only from completed months; 31 Dec rating not a model input | As expected | Pass |
| TC10 | Model training (`train_model.py`) | Champion chosen by the pre-set rule; threshold on validation; test scored once | Logistic regression (C 0.05); threshold 0.1840; test AUC 0.792 (0.742-0.840); 173 vs 257 cases flagged | Pass |
| TC11 | Monitoring (`monitor.py`) | Jul-Sep stable; drift detected from Oct; status per month; decision rule applied | Jul-Sep STABLE; Oct-Nov INVESTIGATE; Dec ACT (portfolio PSI 0.292); trigger not met | Pass |
| TC12 | Power BI export (`export_powerbi.py`) | 12 CSV files | 12 files written | Pass |
| TC13 | Dashboard page 1: data quality | Cards 11 / 67 / 67 / 0; clicking a bar filters the exception table | As expected | Pass |
| TC14 | Dashboard page 2: review queue | Cards 2,438 / 170 / 531; table starts at CUST02635 | As expected after refresh; P1 shown in red | Pass |
| TC15 | Dashboard page 3: FATF matrix | 4 list versions; Nigeria not listed in 2025-10; Iran, Myanmar, DPRK call for action | As expected after D2 fix | Pass |
| TC16 | Dashboard page 4: model and monitoring | Drift crosses 0.1 in Oct and 0.25 in Dec; AUC above 0.751 every month | Portfolio PSI 0.10 (Oct), 0.29 (Dec); lowest AUC 0.785 (Dec) | Pass |
| TC17 | Automated tests (`pytest`) | 57 passed | 57 passed in 5.09 s (13 Nov 2026, 21:26) | Pass |
| TC18 | CI on GitHub (`.github/workflows/ci.yml`) | Full pipeline rebuilt from scratch, Power BI export produced, all tests pass | Run #3 (commit b1611d6, after D5 fix) passed in 1 min 13 s: pipeline rebuilt, export produced, 57 tests passed | Pass |

## 2. Automated tests (`tests/`)

| File | What it checks |
|---|---|
| `test_reference_data.py` (6 tests) | FATF versions and dates; call-for-action list; increased-monitoring list after each published change; UK high-risk flag; rules table complete, with illustrative thresholds labelled; 67 planted issues |
| `test_metrics.py` (9 tests) | PSI is 0 for identical data, high for a shifted one, ignores missing values; PSI bands; threshold gives target recall with the least workload |
| `test_exports.py` (13 tests) | Every Power BI export file exists and has the columns the dashboard uses; the exported answer-key test shows 67 matched, 0 missed or extra |
| `test_pipeline_db.py` (29 tests) | Row counts file → RAW → STAGING; invalid ratings not auto-corrected; data-quality checks match the answer key; activity rules R06-R09 and overdue rule R12 match an independent pandas recomputation; FATF status point-in-time (Nigeria, Virgin Islands (UK)); every sanctions hit is P1; periods do not overlap; activity features come from the month before the review; regression test for D1; information cutoff (case rules use only months completed before the review; trigger reviews come from the previous month's alerts; the 31 Dec rating is not a model input; R01 fires only when the country is listed on the review date); every case scored once; threshold matches rules recall with less workload; no drift before deployment; December breach without retraining trigger |

The database tests are skipped when `PGPASSWORD` is not set, and the export tests when the export has not been run.

## 3. Defects found

| ID | Found by | Description | Fix | Retest |
|---|---|---|---|---|
| D1 | Monitoring (TC11): PSI 0.35 on `log_last_turnover_ratio` in July, before any drift existed | `ln(GREATEST(ratio, 0.05))`: PostgreSQL's GREATEST ignores NULL, so cases with no earlier activity (all January cases, new customers) were given "very low turnover" instead of "unknown". The model was trained on this wrong value | Keep missing history NULL (`CASE WHEN ratio IS NOT NULL ...`); retrained | Jul-Sep input PSI ≤ 0.048; validation AUC 0.780 → 0.785; test AUC 0.756 → 0.762; regression test added |
| D2 | Dashboard check against FATF lists (TC15) | Power BI read `list_version` values such as "2024-10" as dates and grouped them by year, so the matrix showed 2 columns and wrong statuses (e.g. Nigeria listed in late 2025) | Set `list_version` to Text in Power Query | Matrix shows 4 versions; all transitions correct |
| D3 | Building relationships | `queue_reasons` loaded without column names (Column1, Column2 …) because every column is text | Power Query: Use First Row as Headers | Columns named; relationships created |
| D4 | Model view review | Power BI auto-created relationships on unrelated columns (monitoring tables joined on `auc`; rules tables on `description`) | Deleted; created 5 relationships on keys (`customer_id`, `rule_code`, `check_name`) | Filters work as intended (TC13) |
| D5 | Independent review of the repository | The rules baseline and the simulated outcome used the whole review month's activity, including days after the review date, while the model used only earlier months, so the comparison was not like for like. The model also used the 31 Dec 2025 risk rating | One information cutoff for rules, trigger reviews, features and outcome; activity extended back to Oct 2024; rating removed from the model; data regenerated and all results re-run | 4 regression tests added; 57 tests pass; test AUC 0.792; Oct-Dec model recall 0.717 vs rules 0.674 (earlier result reversed) |
| D6 | Independent review | R10 (expired documents) was labelled a statutory requirement | Relabelled as bank judgement applying the duty to keep CDD up to date | README and dashboard show R10 as bank judgement |

## 4. Known limitations
- Customers, activity and escalation outcomes are synthetic; results show the method, not real-world accuracy.
- Documents and shareholder registers exist only as a 31 Dec 2025 snapshot.
- No sanctions-match resolution field: every screening hit is treated as unresolved.
- Monthly activity is aggregated, so every review uses only months completed before the review month.
