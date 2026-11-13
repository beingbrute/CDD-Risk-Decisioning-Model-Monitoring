-- =====================================================================
-- 03_risk_rules.sql
-- The 12 CDD rules (staging.cdd_rules), applied in one place to two kinds of
-- "evaluation":
--   PORTFOLIO  every customer as at the as-at date (31 Dec 2025), using
--              December 2025 activity       -> feeds the review queue
--   CASE       every analyst review case on its own review date, using the last
--              month completed before the review -> feeds rule effectiveness
--              (the review month's own totals include days after the review date)
--
-- Country status is point-in-time: a country counts as a UK high-risk third
-- country only if it was on a FATF list on the evaluation date
-- (MLR 2017 reg. 33(3)(a) as amended by SI 2024/69).
--
-- Known simplifications (stated in the README):
--   * Documents and shareholder registers exist only as a 31 Dec 2025 snapshot,
--     so R10 and R11 use that snapshot for every evaluation; R10 does compare
--     expiry dates with the evaluation date.
--   * The data has no sanctions-match resolution field, so every screening hit
--     is treated as unresolved (R04).
--   * R12 (overdue review) is evaluated for the portfolio only.
-- =====================================================================

-- ---------- thresholds (illustrative bank policy values; change here to tune) ----------
DROP TABLE IF EXISTS analytics.rule_parameters CASCADE;
CREATE TABLE analytics.rule_parameters (
    parameter text PRIMARY KEY, value numeric, used_by text, note text
);
INSERT INTO analytics.rule_parameters VALUES
('turnover_multiple',     2.0,  'R06', 'Activity above this multiple of declared expected monthly turnover'),
('cash_share_max',        0.30, 'R07', 'Cash share of monthly activity'),
('hrtc_flow_share_max',   0.10, 'R08', 'Share of flows to or from FATF-listed jurisdictions'),
('tm_alerts_min',         2,    'R09', 'Transaction-monitoring alerts in the month'),
('bo_threshold_pct',      25,   'R11', 'Beneficial owner: more than this % (MLR 2017 reg. 5(1)(b))');

-- ---------- what is evaluated, when ----------
DROP TABLE IF EXISTS analytics.rule_evaluations CASCADE;
CREATE TABLE analytics.rule_evaluations AS
SELECT 'PORTFOLIO'::text AS eval_type,
       'ASOF-' || c.customer_id AS eval_id,
       c.customer_id,
       p.as_of_date AS eval_date,
       date_trunc('month', p.as_of_date)::date AS activity_month
FROM staging.customers c CROSS JOIN staging.run_parameters p
UNION ALL
SELECT 'CASE', r.case_id, r.customer_id, r.review_date, (r.review_month - INTERVAL '1 month')::date
FROM staging.review_cases r
WHERE EXISTS (SELECT 1 FROM staging.customers c WHERE c.customer_id = r.customer_id);
ALTER TABLE analytics.rule_evaluations ADD PRIMARY KEY (eval_id);

-- ---------- FATF status on any date ----------
CREATE OR REPLACE FUNCTION analytics.fatf_status_on(p_country text, p_date date) RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT COALESCE(
        (SELECT fatf_status FROM staging.country_risk_history
          WHERE country_code = p_country
            AND p_date >= effective_from
            AND p_date <= COALESCE(effective_to, DATE '9999-12-31')),
        CASE WHEN p_country IS NULL THEN NULL ELSE 'Not listed' END)
$$;

-- ---------- rule hits: one row per evaluation per rule that fires ----------
DROP TABLE IF EXISTS analytics.rule_hits CASCADE;
CREATE TABLE analytics.rule_hits AS
WITH e AS (
    SELECT e.*, c.customer_type, c.country_code, c.pep_flag, c.adverse_media_flag,
           c.screening_hit_flag, c.expected_monthly_turnover, c.next_review_due,
           a.txn_value, a.cash_share, a.high_risk_country_share, a.tm_alerts,
           analytics.fatf_status_on(c.country_code, e.eval_date) AS country_status,
           (SELECT home_country FROM staging.run_parameters) AS home_country
    FROM analytics.rule_evaluations e
    JOIN staging.customers c USING (customer_id)
    LEFT JOIN staging.monthly_activity a
           ON a.customer_id = e.customer_id AND a.month_start = e.activity_month
),
prm AS (SELECT parameter, value FROM analytics.rule_parameters),
bo AS (
    SELECT * FROM staging.company_shareholders
    WHERE ownership_pct > (SELECT value FROM prm WHERE parameter = 'bo_threshold_pct')
)
-- R01 customer established in a UK high-risk third country on the evaluation date
SELECT eval_id, eval_type, customer_id, 'R01' AS rule_code,
       country_code || ': ' || country_status AS detail
FROM e WHERE country_status IN ('Increased monitoring', 'Call for action')

UNION ALL -- R02 beneficial owner established in a FATF-listed jurisdiction
SELECT e.eval_id, e.eval_type, e.customer_id, 'R02',
       string_agg(DISTINCT b.shareholder_country_code || ' (' || b.ownership_pct || '%)', ', ')
FROM e JOIN bo b ON b.customer_id = e.customer_id
WHERE analytics.fatf_status_on(b.shareholder_country_code, e.eval_date) IN ('Increased monitoring', 'Call for action')
GROUP BY e.eval_id, e.eval_type, e.customer_id

UNION ALL -- R03 customer or beneficial owner is a PEP
SELECT e.eval_id, e.eval_type, e.customer_id, 'R03',
       CASE WHEN e.pep_flag = 1 AND e.country_code = e.home_country THEN 'domestic PEP (reg. 35(3A))'
            WHEN e.pep_flag = 1 THEN 'non-domestic PEP'
            ELSE 'beneficial owner is a PEP' END
FROM e
WHERE e.pep_flag = 1
   OR EXISTS (SELECT 1 FROM bo b WHERE b.customer_id = e.customer_id AND b.shareholder_pep_flag = 1)

UNION ALL -- R04 unresolved potential sanctions match
SELECT eval_id, eval_type, customer_id, 'R04', 'potential match, unresolved'
FROM e WHERE screening_hit_flag = 1

UNION ALL -- R05 adverse media
SELECT eval_id, eval_type, customer_id, 'R05', 'adverse media flag'
FROM e WHERE adverse_media_flag = 1

UNION ALL -- R06 activity above the turnover multiple
SELECT eval_id, eval_type, customer_id, 'R06',
       round(txn_value / NULLIF(expected_monthly_turnover, 0), 2) || 'x expected turnover'
FROM e
WHERE txn_value > (SELECT value FROM prm WHERE parameter = 'turnover_multiple') * expected_monthly_turnover

UNION ALL -- R07 cash share
SELECT eval_id, eval_type, customer_id, 'R07', round(cash_share * 100, 1) || '% cash'
FROM e WHERE cash_share > (SELECT value FROM prm WHERE parameter = 'cash_share_max')

UNION ALL -- R08 flows to or from FATF-listed jurisdictions
SELECT eval_id, eval_type, customer_id, 'R08', round(high_risk_country_share * 100, 1) || '% high-risk-country flows'
FROM e WHERE high_risk_country_share > (SELECT value FROM prm WHERE parameter = 'hrtc_flow_share_max')

UNION ALL -- R09 transaction-monitoring alerts
SELECT eval_id, eval_type, customer_id, 'R09', tm_alerts || ' alerts'
FROM e WHERE tm_alerts >= (SELECT value FROM prm WHERE parameter = 'tm_alerts_min')

UNION ALL -- R10 document missing, pending or expired on the evaluation date
SELECT e.eval_id, e.eval_type, e.customer_id, 'R10',
       string_agg(d.doc_type || ': ' ||
                  CASE WHEN d.status IN ('Missing', 'Pending') THEN lower(d.status)
                       ELSE 'expired ' || d.expiry_date END, '; ')
FROM e JOIN staging.documents d ON d.customer_id = e.customer_id
WHERE d.status IN ('Missing', 'Pending') OR d.expiry_date < e.eval_date
GROUP BY e.eval_id, e.eval_type, e.customer_id

UNION ALL -- R11 company: no register, or a beneficial owner not verified
SELECT e.eval_id, e.eval_type, e.customer_id, 'R11',
       CASE WHEN NOT EXISTS (SELECT 1 FROM staging.company_shareholders s WHERE s.customer_id = e.customer_id)
            THEN 'no shareholder register' ELSE 'beneficial owner not verified' END
FROM e
WHERE e.customer_type = 'Company'
  AND (NOT EXISTS (SELECT 1 FROM staging.company_shareholders s WHERE s.customer_id = e.customer_id)
       OR EXISTS (SELECT 1 FROM bo b WHERE b.customer_id = e.customer_id AND b.verified = 0))

UNION ALL -- R12 periodic review overdue (portfolio only)
SELECT eval_id, eval_type, customer_id, 'R12',
       (eval_date - next_review_due) || ' days overdue'
FROM e WHERE eval_type = 'PORTFOLIO' AND next_review_due < eval_date;

CREATE INDEX ON analytics.rule_hits (eval_id);
CREATE INDEX ON analytics.rule_hits (rule_code);

-- ---------- result shown when the script runs ----------
SELECT r.rule_code,
       left(r.description, 60) AS description,
       COUNT(*) FILTER (WHERE h.eval_type = 'PORTFOLIO') AS customers_flagged_now,
       COUNT(*) FILTER (WHERE h.eval_type = 'CASE')      AS review_cases_flagged
FROM staging.cdd_rules r
LEFT JOIN analytics.rule_hits h USING (rule_code)
GROUP BY r.rule_code, r.description
ORDER BY r.rule_code;
