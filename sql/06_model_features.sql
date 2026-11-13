-- =====================================================================
-- 06_model_features.sql
-- One row per review case: the information available on the review date,
-- plus the outcome (analyst_escalated) and the period the case belongs to.
--
-- Leakage rules:
--   * Activity features use only COMPLETED months before the review month
--     (the latest full month and the three before it). The review month's own
--     totals include days after the review date, so they are not used.
--   * Country and beneficial-owner country status are taken on the review date.
--   * Rule flags R01-R05, R10, R11 are used as features. Activity rules R06-R09
--     are not, because the model already sees the same activity as raw values.
--   * The bank's risk rating is kept in this table only for a retrospective
--     baseline: it is the rating as at 31 Dec 2025, so the model does not use it.
--   * Documents and the shareholder register exist only as a 31 Dec 2025
--     snapshot (limitation stated in the README).
--
-- Periods (decided before any model was trained):
--   train       Jan-Jun 2025   fit the models
--   validation  Jul-Aug 2025   choose the model and the threshold
--   test        Sep 2025       final untouched test, used once
--   monitoring  Oct-Dec 2025   simulated post-deployment monitoring
-- =====================================================================

DROP TABLE IF EXISTS analytics.industry_risk CASCADE;
CREATE TABLE analytics.industry_risk (occupation_or_industry text PRIMARY KEY, industry_risk int, cash_intensive int);
-- Illustrative bank-policy tiers: 0 standard, 1 elevated, 2 high
INSERT INTO analytics.industry_risk VALUES
('Salaried', 0, 0), ('Retired', 0, 0), ('Student', 0, 0),
('Self-employed', 1, 0), ('Business owner', 1, 0),
('Retail', 0, 0), ('Professional services', 0, 0), ('Manufacturing', 0, 0),
('Construction', 1, 0), ('Real estate', 1, 0), ('Hospitality', 1, 1),
('Money services', 2, 1), ('Car dealership', 2, 1), ('Crypto services', 2, 0);

DROP TABLE IF EXISTS analytics.model_features CASCADE;
CREATE TABLE analytics.model_features AS
WITH cases AS (
    SELECT r.case_id, r.customer_id, r.review_date, r.review_month, r.review_type, r.analyst_escalated
    FROM staging.review_cases r
    JOIN staging.customers c USING (customer_id)
),
hist AS (   -- completed months before the review month
    SELECT k.case_id,
           a.month_start,
           a.txn_value / NULLIF(c.expected_monthly_turnover, 0) AS turnover_ratio,
           a.cash_share, a.cross_border_share, a.high_risk_country_share, a.tm_alerts,
           (k.review_month - a.month_start) / 28 AS months_back              -- 1 = previous month
    FROM cases k
    JOIN staging.customers c USING (customer_id)
    JOIN staging.monthly_activity a
      ON a.customer_id = k.customer_id
     AND a.month_start <  k.review_month
     AND a.month_start >= (k.review_month - INTERVAL '4 months')::date
    WHERE a.txn_value >= 0                                                -- negative values are DQ errors
),
act AS (
    SELECT case_id,
           MAX(turnover_ratio)          FILTER (WHERE months_back = 1) AS last_turnover_ratio,
           MAX(cash_share)              FILTER (WHERE months_back = 1) AS last_cash_share,
           MAX(cross_border_share)      FILTER (WHERE months_back = 1) AS last_cross_border_share,
           MAX(high_risk_country_share) FILTER (WHERE months_back = 1) AS last_hrtc_flow_share,
           MAX(tm_alerts)               FILTER (WHERE months_back = 1) AS last_tm_alerts,
           AVG(turnover_ratio)          FILTER (WHERE months_back BETWEEN 2 AND 4) AS prior3_turnover_ratio,
           SUM(tm_alerts)               FILTER (WHERE months_back BETWEEN 1 AND 4) AS alerts_4m,
           MAX(cash_share)              FILTER (WHERE months_back BETWEEN 1 AND 4) AS max_cash_share_4m,
           COUNT(*)                                                         AS months_of_history
    FROM hist GROUP BY case_id
),
rules AS (
    SELECT h.eval_id AS case_id,
           MAX((h.rule_code = 'R01')::int) AS r01_hrtc_customer,
           MAX((h.rule_code = 'R02')::int) AS r02_hrtc_owner,
           MAX((h.rule_code = 'R03')::int) AS r03_pep,
           MAX((h.rule_code = 'R04')::int) AS r04_sanctions_hit,
           MAX((h.rule_code = 'R05')::int) AS r05_adverse_media,
           MAX((h.rule_code = 'R10')::int) AS r10_document_gap,
           MAX((h.rule_code = 'R11')::int) AS r11_ownership_gap
    FROM analytics.rule_hits h
    WHERE h.eval_type = 'CASE' AND h.rule_code IN ('R01','R02','R03','R04','R05','R10','R11')
    GROUP BY h.eval_id
)
SELECT k.case_id, k.customer_id, k.review_date, k.review_month,
       CASE WHEN k.review_month <  DATE '2025-07-01' THEN 'train'
            WHEN k.review_month <  DATE '2025-09-01' THEN 'validation'
            WHEN k.review_month <  DATE '2025-10-01' THEN 'test'
            ELSE 'monitoring' END                                   AS period,
       -- customer profile
       (c.customer_type = 'Company')::int                          AS is_company,
       c.channel,
       (c.channel = 'Online')::int                                 AS channel_online,
       (c.channel = 'Intermediary')::int                           AS channel_intermediary,
       COALESCE(i.industry_risk, 0)                                AS industry_risk,
       COALESCE(i.cash_intensive, 0)                               AS cash_intensive,
       (k.review_date - c.onboard_date) / 365.25                   AS tenure_years,
       CASE c.risk_rating WHEN 'Low' THEN 0 WHEN 'Medium' THEN 1 WHEN 'High' THEN 2 END AS bank_risk_rating,
       (c.pep_flag = 1 AND c.country_code = p.home_country)::int   AS domestic_pep,
       (c.pep_flag = 1 AND c.country_code <> p.home_country)::int  AS non_domestic_pep,
       (k.review_type = 'Trigger')::int                            AS trigger_review,
       -- point-in-time rule flags
       COALESCE(r.r01_hrtc_customer, 0) AS r01_hrtc_customer,
       COALESCE(r.r02_hrtc_owner, 0)    AS r02_hrtc_owner,
       COALESCE(r.r03_pep, 0)           AS r03_pep,
       COALESCE(r.r04_sanctions_hit, 0) AS r04_sanctions_hit,
       COALESCE(r.r05_adverse_media, 0) AS r05_adverse_media,
       COALESCE(r.r10_document_gap, 0)  AS r10_document_gap,
       COALESCE(r.r11_ownership_gap, 0) AS r11_ownership_gap,
       -- activity from completed months only (NULL if the customer is new)
       -- GREATEST() ignores NULLs in PostgreSQL, so missing history must stay NULL explicitly
       CASE WHEN a.last_turnover_ratio IS NOT NULL
            THEN ln(GREATEST(a.last_turnover_ratio, 0.05)) END    AS log_last_turnover_ratio,
       CASE WHEN a.prior3_turnover_ratio IS NOT NULL
            THEN ln(GREATEST(a.prior3_turnover_ratio, 0.05)) END  AS log_prior3_turnover_ratio,
       a.last_cash_share, a.max_cash_share_4m,
       a.last_cross_border_share, a.last_hrtc_flow_share,
       a.last_tm_alerts, a.alerts_4m,
       COALESCE(a.months_of_history, 0)                            AS months_of_history,
       -- outcome
       k.analyst_escalated
FROM cases k
JOIN staging.customers c USING (customer_id)
CROSS JOIN staging.run_parameters p
LEFT JOIN analytics.industry_risk i USING (occupation_or_industry)
LEFT JOIN act a USING (case_id)
LEFT JOIN rules r USING (case_id);
ALTER TABLE analytics.model_features ADD PRIMARY KEY (case_id);

-- ---------- result shown when the script runs ----------
SELECT period,
       MIN(review_date) AS first_review, MAX(review_date) AS last_review,
       COUNT(*) AS cases, SUM(analyst_escalated) AS escalated,
       round(AVG(analyst_escalated), 3) AS escalation_rate,
       COUNT(*) FILTER (WHERE months_of_history = 0) AS no_activity_history
FROM analytics.model_features
GROUP BY period
ORDER BY MIN(review_date);
