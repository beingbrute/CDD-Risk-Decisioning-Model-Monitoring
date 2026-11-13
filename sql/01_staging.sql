-- =====================================================================
-- 01_staging.sql
-- RAW (all text, as delivered)  ->  STAGING (typed, trimmed, flagged)
--
-- Rules for this layer:
--   * Every raw row is kept. Nothing is deleted or silently corrected.
--   * Values are converted to proper types with safe casts: a value that
--     cannot be converted becomes NULL instead of stopping the load.
--   * The original text of fields that matter for checks is kept next to
--     the cleaned value (for example risk_rating_raw), so the data-quality
--     checks in 02_dq_checks.sql can see exactly what was received.
--   * A risk rating is a decision, so it is not auto-corrected: only the
--     exact values Low / Medium / High are accepted.
-- =====================================================================

-- ---------- run parameters ----------
DROP TABLE IF EXISTS staging.run_parameters CASCADE;
CREATE TABLE staging.run_parameters AS
SELECT DATE '2025-12-31' AS as_of_date,          -- data is assessed as at this date
       'GB'::text        AS home_country;        -- domestic = United Kingdom

-- ---------- safe-cast helpers ----------
CREATE OR REPLACE FUNCTION staging.try_date(t text) RETURNS date
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    RETURN NULLIF(btrim(t), '')::date;
EXCEPTION WHEN others THEN
    RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION staging.try_numeric(t text) RETURNS numeric
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    RETURN NULLIF(btrim(t), '')::numeric;
EXCEPTION WHEN others THEN
    RETURN NULL;
END $$;

-- ---------- reference data ----------
DROP TABLE IF EXISTS staging.country_risk_history CASCADE;
CREATE TABLE staging.country_risk_history AS
SELECT btrim(country_code)                                 AS country_code,
       btrim(country_name)                                 AS country_name,
       btrim(fatf_status)                                  AS fatf_status,
       staging.try_numeric(uk_high_risk_third_country)::int AS uk_high_risk_third_country,
       btrim(list_version)                                 AS list_version,
       staging.try_date(effective_from)                    AS effective_from,
       staging.try_date(effective_to)                      AS effective_to,   -- NULL = still in force
       btrim(source)                                       AS source
FROM raw.country_risk_history;

DROP TABLE IF EXISTS staging.cdd_rules CASCADE;
CREATE TABLE staging.cdd_rules AS
SELECT btrim(rule_code) AS rule_code, description, next_action, legal_basis, basis_type
FROM raw.cdd_rules;

-- ---------- customers: every raw row, with a duplicate flag ----------
DROP TABLE IF EXISTS staging.customers_all CASCADE;
CREATE TABLE staging.customers_all AS
SELECT btrim(customer_id)                               AS customer_id,
       btrim(customer_type)                             AS customer_type,
       staging.try_date(onboard_date)                   AS onboard_date,
       NULLIF(btrim(country_code), '')                  AS country_code,
       btrim(occupation_or_industry)                    AS occupation_or_industry,
       btrim(channel)                                   AS channel,
       staging.try_numeric(pep_flag)::int               AS pep_flag,
       staging.try_numeric(adverse_media_flag)::int     AS adverse_media_flag,
       staging.try_numeric(screening_hit_flag)::int     AS screening_hit_flag,
       staging.try_numeric(expected_monthly_turnover)   AS expected_monthly_turnover,
       risk_rating                                      AS risk_rating_raw,
       CASE WHEN risk_rating IN ('Low', 'Medium', 'High')
            THEN risk_rating END                        AS risk_rating,      -- NULL if not exactly valid
       staging.try_date(last_review_date)               AS last_review_date,
       staging.try_date(next_review_due)                AS next_review_due,
       COUNT(*) OVER (PARTITION BY btrim(customer_id))  AS id_occurrences
FROM raw.customers;

-- one row per customer for everything downstream (duplicates here are exact copies)
DROP TABLE IF EXISTS staging.customers CASCADE;
CREATE TABLE staging.customers AS
SELECT DISTINCT ON (customer_id) *
FROM staging.customers_all
ORDER BY customer_id;
ALTER TABLE staging.customers ADD PRIMARY KEY (customer_id);

-- ---------- company shareholder register ----------
DROP TABLE IF EXISTS staging.company_shareholders CASCADE;
CREATE TABLE staging.company_shareholders AS
SELECT btrim(customer_id)                            AS customer_id,
       btrim(shareholder_id)                         AS shareholder_id,
       staging.try_numeric(ownership_pct)            AS ownership_pct,
       NULLIF(btrim(shareholder_country_code), '')   AS shareholder_country_code,
       staging.try_numeric(shareholder_pep_flag)::int AS shareholder_pep_flag,
       staging.try_numeric(verified)::int            AS verified,
       staging.try_numeric(ownership_pct) > 25       AS is_beneficial_owner   -- MLR 2017 reg. 5(1)(b)
FROM raw.company_shareholders;

-- ---------- documents ----------
DROP TABLE IF EXISTS staging.documents CASCADE;
CREATE TABLE staging.documents AS
SELECT btrim(customer_id)           AS customer_id,
       btrim(doc_type)              AS doc_type,
       btrim(status)                AS status,
       staging.try_date(issue_date)  AS issue_date,
       staging.try_date(expiry_date) AS expiry_date            -- NULL = no expiry (e.g. proof of address)
FROM raw.documents;

-- ---------- monthly activity ----------
DROP TABLE IF EXISTS staging.monthly_activity CASCADE;
CREATE TABLE staging.monthly_activity AS
SELECT btrim(customer_id)                            AS customer_id,
       staging.try_date(month || '-01')              AS month_start,
       staging.try_numeric(txn_count)::int           AS txn_count,
       staging.try_numeric(txn_value)                AS txn_value,
       staging.try_numeric(cash_share)               AS cash_share,
       staging.try_numeric(cross_border_share)       AS cross_border_share,
       staging.try_numeric(high_risk_country_share)  AS high_risk_country_share,
       staging.try_numeric(tm_alerts)::int           AS tm_alerts
FROM raw.monthly_activity;

-- ---------- review cases ----------
DROP TABLE IF EXISTS staging.review_cases CASCADE;
CREATE TABLE staging.review_cases AS
SELECT btrim(case_id)                             AS case_id,
       btrim(customer_id)                         AS customer_id,
       staging.try_date(review_date)              AS review_date,
       staging.try_date(review_month || '-01')    AS review_month,
       btrim(review_type)                         AS review_type,
       staging.try_numeric(analyst_escalated)::int AS analyst_escalated
FROM raw.review_cases;
ALTER TABLE staging.review_cases ADD PRIMARY KEY (case_id);

-- ---------- answer key (used only to test the checks) ----------
DROP TABLE IF EXISTS staging.expected_issues CASCADE;
CREATE TABLE staging.expected_issues AS
SELECT btrim("check") AS check_name, btrim("table") AS table_name, btrim(key) AS record_key
FROM raw.expected_issues;

-- ---------- reconciliation: raw rows in = staging rows out ----------
SELECT t.table_name,
       t.raw_rows,
       t.staging_rows,
       t.unparsed_values,
       CASE WHEN t.raw_rows = t.staging_rows AND t.unparsed_values = 0 THEN 'OK' ELSE 'CHECK' END AS status
FROM (
    SELECT 'customers' AS table_name,
           (SELECT COUNT(*) FROM raw.customers) AS raw_rows,
           (SELECT COUNT(*) FROM staging.customers_all) AS staging_rows,
           (SELECT COUNT(*) FROM staging.customers_all
             WHERE onboard_date IS NULL OR last_review_date IS NULL OR next_review_due IS NULL
                OR expected_monthly_turnover IS NULL OR pep_flag IS NULL) AS unparsed_values
    UNION ALL
    SELECT 'company_shareholders', (SELECT COUNT(*) FROM raw.company_shareholders),
           (SELECT COUNT(*) FROM staging.company_shareholders),
           (SELECT COUNT(*) FROM staging.company_shareholders WHERE ownership_pct IS NULL)
    UNION ALL
    SELECT 'documents', (SELECT COUNT(*) FROM raw.documents), (SELECT COUNT(*) FROM staging.documents),
           (SELECT COUNT(*) FROM staging.documents d JOIN raw.documents r
               ON r.customer_id = d.customer_id AND r.doc_type = d.doc_type
             WHERE d.issue_date IS NULL OR (d.expiry_date IS NULL AND NULLIF(r.expiry_date, '') IS NOT NULL))
    UNION ALL
    SELECT 'monthly_activity', (SELECT COUNT(*) FROM raw.monthly_activity),
           (SELECT COUNT(*) FROM staging.monthly_activity),
           (SELECT COUNT(*) FROM staging.monthly_activity WHERE month_start IS NULL OR txn_value IS NULL)
    UNION ALL
    SELECT 'review_cases', (SELECT COUNT(*) FROM raw.review_cases), (SELECT COUNT(*) FROM staging.review_cases),
           (SELECT COUNT(*) FROM staging.review_cases WHERE review_date IS NULL OR analyst_escalated IS NULL)
    UNION ALL
    SELECT 'country_risk_history', (SELECT COUNT(*) FROM raw.country_risk_history),
           (SELECT COUNT(*) FROM staging.country_risk_history),
           (SELECT COUNT(*) FROM staging.country_risk_history WHERE effective_from IS NULL)
) t;
