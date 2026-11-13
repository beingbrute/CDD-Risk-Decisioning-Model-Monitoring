-- =====================================================================
-- 02_dq_checks.sql
-- CDD data-quality checks on the STAGING layer.
--
-- Output (ANALYTICS schema):
--   dq_check_catalogue   what each check tests, its severity and why it matters
--   dq_exceptions        one row per failing record: the records to fix
--   dq_summary           one row per check: records failed, status
--   dq_answer_key_test   the checks compared with the planted-issue answer key
--
-- Severity: 'error'   = CDD record is wrong or incomplete, must be fixed
--           'warning' = internal policy-consistency exception, must be reviewed
-- =====================================================================

DROP TABLE IF EXISTS analytics.dq_check_catalogue CASCADE;
CREATE TABLE analytics.dq_check_catalogue (
    check_name  text PRIMARY KEY,
    table_name  text,
    severity    text,
    description text,
    why_it_matters text
);
INSERT INTO analytics.dq_check_catalogue VALUES
('duplicate_customer_id', 'customers', 'error',
 'Customer ID appears more than once',
 'One customer must have one CDD record; duplicates split the history and the risk view'),
('missing_country_code', 'customers', 'error',
 'Country of residence or establishment is blank',
 'Country decides whether the high-risk third country rule (R01) applies'),
('invalid_risk_rating', 'customers', 'error',
 'Risk rating is not exactly Low, Medium or High',
 'The rating sets the review cycle and the level of due diligence'),
('pep_rating_policy_exception', 'customers', 'warning',
 'PEP rated below internal policy (non-domestic PEP not High, or domestic PEP rated Low)',
 'Internal policy consistency. The regulations require EDD for PEPs, not a rating label'),
('review_date_in_future', 'customers', 'error',
 'Last review date is after the as-at date',
 'A review cannot have happened in the future; the review history is unreliable'),
('review_cycle_mismatch', 'customers', 'error',
 'Next review due date does not match the rating''s review cycle (High 1, Medium 2, Low 3 years of 365 days)',
 'Reviews could be scheduled too late for the customer''s risk'),
('company_without_register', 'customers', 'error',
 'Company customer has no shareholder register',
 'Beneficial owners cannot be identified (MLR 2017 reg. 28(4))'),
('register_not_100', 'company_shareholders', 'error',
 'Shareholdings in a company''s complete register do not add up to 100%',
 'The register is incomplete or wrong, so beneficial owners may be missed'),
('verified_doc_expired', 'documents', 'error',
 'Document marked Verified but its expiry date has passed',
 'Status is misleading; CDD documents must be kept up to date (reg. 28(11)(b))'),
('negative_txn_value', 'monthly_activity', 'error',
 'Monthly transaction value is negative',
 'Activity totals and the turnover rule (R06) would be wrong'),
('activity_orphan_customer', 'monthly_activity', 'error',
 'Activity for a customer ID that is not in the customer table',
 'Activity cannot be linked to a CDD record');

-- ---------- exceptions: one row per failing record ----------
DROP TABLE IF EXISTS analytics.dq_exceptions CASCADE;
CREATE TABLE analytics.dq_exceptions AS
WITH p AS (SELECT * FROM staging.run_parameters),
yrs AS (SELECT * FROM (VALUES ('High', 1), ('Medium', 2), ('Low', 3)) v(rating, years))

SELECT 'duplicate_customer_id' AS check_name, customer_id AS record_key,
       'appears ' || MAX(id_occurrences) || ' times' AS detail
FROM staging.customers_all WHERE id_occurrences > 1 GROUP BY customer_id

UNION ALL
SELECT 'missing_country_code', customer_id, 'country_code is blank'
FROM staging.customers WHERE country_code IS NULL

UNION ALL
SELECT 'invalid_risk_rating', customer_id, 'risk_rating = "' || COALESCE(risk_rating_raw, '') || '"'
FROM staging.customers WHERE risk_rating IS NULL

UNION ALL
SELECT 'pep_rating_policy_exception', c.customer_id,
       CASE WHEN c.country_code = p.home_country THEN 'domestic PEP rated ' ELSE 'non-domestic PEP rated ' END
       || c.risk_rating
FROM staging.customers c CROSS JOIN p
WHERE c.pep_flag = 1
  AND ((c.country_code = p.home_country AND c.risk_rating = 'Low')
    OR (c.country_code <> p.home_country AND c.risk_rating <> 'High'))

UNION ALL
SELECT 'review_date_in_future', c.customer_id, 'last_review_date = ' || c.last_review_date
FROM staging.customers c CROSS JOIN p WHERE c.last_review_date > p.as_of_date

UNION ALL
SELECT 'review_cycle_mismatch', c.customer_id,
       c.risk_rating || ': expected ' || (c.last_review_date + y.years * 365) || ', recorded ' || c.next_review_due
FROM staging.customers c JOIN yrs y ON y.rating = c.risk_rating
WHERE c.next_review_due <> c.last_review_date + y.years * 365

UNION ALL
SELECT 'company_without_register', c.customer_id, 'no shareholders recorded'
FROM staging.customers c
WHERE c.customer_type = 'Company'
  AND NOT EXISTS (SELECT 1 FROM staging.company_shareholders s WHERE s.customer_id = c.customer_id)

UNION ALL
SELECT 'register_not_100', customer_id, 'shares total ' || SUM(ownership_pct) || '%'
FROM staging.company_shareholders GROUP BY customer_id
HAVING ABS(SUM(ownership_pct) - 100) > 0.01

UNION ALL
SELECT 'verified_doc_expired', d.customer_id, d.doc_type || ' expired ' || d.expiry_date
FROM staging.documents d CROSS JOIN p
WHERE d.status = 'Verified' AND d.expiry_date < p.as_of_date

UNION ALL
SELECT 'negative_txn_value', customer_id,
       string_agg(to_char(month_start, 'YYYY-MM') || ': ' || txn_value, '; ')
FROM staging.monthly_activity WHERE txn_value < 0 GROUP BY customer_id

UNION ALL
SELECT 'activity_orphan_customer', a.customer_id, COUNT(*) || ' activity row(s)'
FROM staging.monthly_activity a
WHERE NOT EXISTS (SELECT 1 FROM staging.customers c WHERE c.customer_id = a.customer_id)
GROUP BY a.customer_id;

-- ---------- summary: one row per check ----------
DROP TABLE IF EXISTS analytics.dq_summary CASCADE;
CREATE TABLE analytics.dq_summary AS
SELECT k.check_name, k.table_name, k.severity,
       COUNT(e.record_key) AS records_failed,
       CASE WHEN COUNT(e.record_key) = 0 THEN 'PASS'
            WHEN k.severity = 'warning' THEN 'WARN'
            ELSE 'FAIL' END AS status,
       k.description
FROM analytics.dq_check_catalogue k
LEFT JOIN analytics.dq_exceptions e ON e.check_name = k.check_name
GROUP BY k.check_name, k.table_name, k.severity, k.description;

-- ---------- test: do the checks find exactly the planted issues? ----------
DROP TABLE IF EXISTS analytics.dq_answer_key_test CASCADE;
CREATE TABLE analytics.dq_answer_key_test AS
SELECT COALESCE(x.check_name, f.check_name)             AS check_name,
       COUNT(x.record_key)                              AS expected,
       COUNT(f.record_key)                              AS found,
       COUNT(*) FILTER (WHERE x.record_key IS NOT NULL AND f.record_key IS NOT NULL) AS matched,
       COUNT(*) FILTER (WHERE f.record_key IS NULL)     AS missed,
       COUNT(*) FILTER (WHERE x.record_key IS NULL)     AS extra
FROM staging.expected_issues x
FULL OUTER JOIN (SELECT DISTINCT check_name, record_key FROM analytics.dq_exceptions) f
  ON f.check_name = x.check_name AND f.record_key = x.record_key
GROUP BY 1;

-- ---------- result shown when the script runs ----------
SELECT check_name, severity, records_failed, status, expected, matched, missed, extra, answer_key
FROM (
    SELECT 1 AS sort_group, s.check_name, s.severity, s.records_failed, s.status,
           t.expected, t.matched, t.missed, t.extra,
           CASE WHEN t.missed = 0 AND t.extra = 0 THEN 'OK' ELSE 'MISMATCH' END AS answer_key
    FROM analytics.dq_summary s
    LEFT JOIN analytics.dq_answer_key_test t USING (check_name)
    UNION ALL
    SELECT 2, 'TOTAL', '', SUM(s.records_failed), '',
           SUM(t.expected), SUM(t.matched), SUM(t.missed), SUM(t.extra),
           CASE WHEN SUM(t.missed) = 0 AND SUM(t.extra) = 0 THEN 'ALL MATCH' ELSE 'MISMATCH' END
    FROM analytics.dq_summary s
    LEFT JOIN analytics.dq_answer_key_test t USING (check_name)
) r
ORDER BY sort_group, check_name;
