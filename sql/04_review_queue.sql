-- =====================================================================
-- 04_review_queue.sql
-- Turns the portfolio rule hits (as at 31 Dec 2025) into a ranked review queue.
--
-- Priority score = sum of rule weights + overdue points (1 per 30 days overdue,
-- capped at 20). The weights are illustrative bank-policy values, kept in a
-- table so they are visible and can be changed.
--
-- Bands:
--   P1  any unresolved sanctions match (R04), or score >= 50
--   P2  score >= 25
--   P3  any other rule hit
-- =====================================================================

DROP TABLE IF EXISTS analytics.rule_weights CASCADE;
CREATE TABLE analytics.rule_weights (rule_code text PRIMARY KEY, weight int, reason text);
INSERT INTO analytics.rule_weights VALUES
('R04', 40, 'Possible sanctions match: no further dealing until resolved'),
('R01', 25, 'Statutory EDD case: high-risk third country'),
('R03', 25, 'Statutory EDD case: PEP'),
('R02', 15, 'Owner in a FATF-listed jurisdiction: geographical risk assessment'),
('R06', 15, 'Unusually large activity: statutory examination of purpose'),
('R08', 15, 'Material flows to or from FATF-listed jurisdictions'),
('R09', 15, 'Repeated monitoring alerts'),
('R11', 15, 'Beneficial ownership not established'),
('R05', 10, 'Adverse media'),
('R07', 10, 'High cash share'),
('R10', 10, 'CDD documents missing, pending or expired'),
('R12', 10, 'Periodic review overdue');

DROP TABLE IF EXISTS analytics.review_queue CASCADE;
CREATE TABLE analytics.review_queue AS
WITH hits AS (
    SELECT h.customer_id, h.rule_code, h.detail, w.weight, r.next_action
    FROM analytics.rule_hits h
    JOIN analytics.rule_weights w USING (rule_code)
    JOIN staging.cdd_rules r USING (rule_code)
    WHERE h.eval_type = 'PORTFOLIO'
),
agg AS (
    SELECT customer_id,
           SUM(weight)                                                 AS rule_points,
           string_agg(rule_code, ', ' ORDER BY rule_code)              AS reason_codes,
           string_agg(rule_code || ' ' || detail, ' | ' ORDER BY rule_code) AS reason_details,
           string_agg(DISTINCT next_action, '; ')                      AS next_actions,
           COUNT(*)                                                    AS rules_hit,
           bool_or(rule_code = 'R04')                                  AS sanctions_hit
    FROM hits GROUP BY customer_id
)
SELECT c.customer_id, c.customer_type, c.country_code, c.channel, c.risk_rating,
       c.last_review_date, c.next_review_due,
       GREATEST(p.as_of_date - c.next_review_due, 0)                    AS days_overdue,
       a.rules_hit, a.reason_codes, a.reason_details, a.next_actions,
       a.rule_points + LEAST(GREATEST(p.as_of_date - c.next_review_due, 0) / 30, 20) AS priority_score,
       CASE WHEN a.sanctions_hit
              OR a.rule_points + LEAST(GREATEST(p.as_of_date - c.next_review_due, 0) / 30, 20) >= 50 THEN 'P1'
            WHEN a.rule_points + LEAST(GREATEST(p.as_of_date - c.next_review_due, 0) / 30, 20) >= 25 THEN 'P2'
            ELSE 'P3' END                                               AS priority
FROM agg a
JOIN staging.customers c USING (customer_id)
CROSS JOIN staging.run_parameters p;

-- rank inside each priority band: highest score first, then most overdue
ALTER TABLE analytics.review_queue ADD COLUMN queue_position int;
UPDATE analytics.review_queue q SET queue_position = r.pos
FROM (SELECT customer_id,
             ROW_NUMBER() OVER (ORDER BY priority, priority_score DESC, days_overdue DESC, customer_id) AS pos
      FROM analytics.review_queue) r
WHERE r.customer_id = q.customer_id;

-- overdue periodic reviews, most overdue first within risk rating
DROP TABLE IF EXISTS analytics.overdue_reviews CASCADE;
CREATE TABLE analytics.overdue_reviews AS
SELECT q.customer_id, q.risk_rating, q.next_review_due, q.days_overdue, q.priority, q.reason_codes
FROM analytics.review_queue q
WHERE q.reason_codes LIKE '%R12%'
ORDER BY CASE q.risk_rating WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 WHEN 'Low' THEN 3 ELSE 4 END,
         q.days_overdue DESC;

-- ---------- result shown when the script runs ----------
SELECT priority,
       COUNT(*)                                     AS customers,
       round(AVG(priority_score), 1)                AS avg_score,
       COUNT(*) FILTER (WHERE days_overdue > 0)     AS overdue,
       (SELECT COUNT(*) FROM staging.customers)     AS portfolio_size
FROM analytics.review_queue
GROUP BY priority
ORDER BY priority;
