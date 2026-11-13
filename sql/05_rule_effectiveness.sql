-- =====================================================================
-- 05_rule_effectiveness.sql
-- How well does each rule pick out the review cases that analysts escalated?
--
-- Evaluated on every 2025 review case on its own review date. The rules are
-- fixed in advance, not fitted to the data, so using all months does not leak
-- information into the model work in step 5.
--
--   flag_rate  share of cases the rule flags (workload it creates)
--   precision  of the cases it flags, share escalated
--   recall     of all escalated cases, share it flags
--   lift       precision / overall escalation rate (1.0 = no better than random)
-- R12 is not evaluated at case level (see 03_risk_rules.sql).
-- =====================================================================

DROP TABLE IF EXISTS analytics.rule_effectiveness CASCADE;
CREATE TABLE analytics.rule_effectiveness AS
WITH cases AS (
    SELECT e.eval_id, r.analyst_escalated
    FROM analytics.rule_evaluations e
    JOIN staging.review_cases r ON r.case_id = e.eval_id
    WHERE e.eval_type = 'CASE'
),
totals AS (
    SELECT COUNT(*) AS n_cases, SUM(analyst_escalated) AS n_escalated FROM cases
),
per_rule AS (
    SELECT h.rule_code, COUNT(*) AS cases_flagged, SUM(c.analyst_escalated) AS escalated_flagged
    FROM analytics.rule_hits h JOIN cases c ON c.eval_id = h.eval_id
    GROUP BY h.rule_code
    UNION ALL
    SELECT 'ANY', COUNT(*), SUM(c.analyst_escalated)          -- at least one rule fired
    FROM cases c WHERE EXISTS (SELECT 1 FROM analytics.rule_hits h WHERE h.eval_id = c.eval_id)
)
SELECT p.rule_code,
       COALESCE(r.description, 'Any rule fires') AS description,
       p.cases_flagged,
       p.escalated_flagged,
       round(p.cases_flagged::numeric / t.n_cases, 3)               AS flag_rate,
       round(p.escalated_flagged::numeric / p.cases_flagged, 3)      AS precision,
       round(p.escalated_flagged::numeric / t.n_escalated, 3)        AS recall,
       round((p.escalated_flagged::numeric / p.cases_flagged)
             / (t.n_escalated::numeric / t.n_cases), 2)              AS lift,
       t.n_cases, t.n_escalated
FROM per_rule p
CROSS JOIN totals t
LEFT JOIN staging.cdd_rules r USING (rule_code);

-- ---------- result shown when the script runs ----------
SELECT rule_code, cases_flagged, escalated_flagged, flag_rate, precision, recall, lift
FROM analytics.rule_effectiveness
ORDER BY rule_code = 'ANY', rule_code;
