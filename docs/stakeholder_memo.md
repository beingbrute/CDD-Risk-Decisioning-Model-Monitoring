# Memo: CDD review findings and escalation model

**To:** Head of CDD Operations
**From:** Aditya Ranjan, Data Analyst
**Date:** 13 November 2026
**Data:** synthetic portfolio of 5,000 customers and 6,374 review cases, January-December 2025; real FATF lists in force during 2025

---

## Summary
- **67 records need correction** before the next review cycle, across 11 data-quality checks.
- **2,447 customers (49%) have at least one CDD rule hit.** 169 are priority 1, including every unresolved sanctions match. **533 periodic reviews are overdue.**
- The rules catch 69% of the cases analysts escalate, but only 24% of the cases they flag are escalated.
- A model tested on September reviews **flagged 39% fewer cases** than the rules, with higher precision (0.36 vs 0.23) and similar recall (0.615 vs 0.648).
- **Online customers' cross-border activity has changed since October.** The model still works, but under this change it caught fewer escalations than the rules. I recommend running it alongside the rules, not instead of them.

## 1. Data quality: fix before reviews
| Issue | Records |
|---|---|
| Photo ID marked Verified but expired | 10 |
| Country missing | 8 |
| Shareholdings not adding up to 100% | 7 |
| Invalid risk rating (e.g. "Hgh", "LOW ") | 6 |
| Next review date inconsistent with the rating | 6 |
| Other (duplicates, missing registers, future review dates, negative values, unknown customers) | 25 |
| PEP rated below internal policy (policy exception, not a regulatory breach) | 5 |

The exception list in the dashboard gives each record and what is wrong with it.

## 2. Review queue
Work the queue in order: P1 (169), then P2 (553), then P3 (1,725). Each customer shows the rules that fired and the next action, for example "refer to sanctions team", "enhanced due diligence", "verify beneficial owners". Overdue reviews (533) are included and ranked by risk rating.

Country status is checked on the review date. Nigeria, South Africa, Mozambique and Burkina Faso left the FATF list on 24 October 2025, so EDD on that ground no longer applies to reviews from that date; the Virgin Islands (UK) have been listed since 13 June 2025.

## 3. Which rules work
- **Most useful:** unusual turnover (R06), flows to FATF-listed jurisdictions (R08) and repeated alerts (R09). Each identifies escalations at more than twice the average rate.
- **Low escalation rate but required by law:** PEP (R03) and beneficial ownership (R11). These stay as they are; a low escalation rate means analysts often clear these cases, which is expected.
- **Open to tuning:** the illustrative thresholds (R06-R09, review cycles). Any change should be tested against past outcomes first.

## 4. Escalation model
- Trained on January-June, chosen on July-August, tested once on September.
- On September cases it flagged 156 cases against 257 for the rules, catching 56 of 91 escalations against 59 for the rules. This is fewer analyst reviews for about the same number of escalations found.
- The main drivers are flows to FATF-listed jurisdictions, whether the review was alert-triggered, and industry risk.
- A simpler model (logistic regression) was kept because a more complex one was not better.

## 5. Monitoring and recommendation
- From October, cross-border activity of Online customers moved away from the January-June pattern (stability index 0.11 in October, 0.29 in December, above the 0.25 action level).
- The model's scores stayed stable and its accuracy stayed above the agreed minimum. But in October-December it caught 58.5% of escalations against 75.1% for the rules (which also flagged more cases: 53% against 29%).

**Recommendations**
1. Correct the 67 data-quality exceptions, starting with the 10 expired documents marked Verified.
2. Clear P1 cases first, then overdue High-risk reviews.
3. Use the model to order the review queue, **alongside** the rules, not to replace them. Statutory rules (R01, R03, R04, R10, R11) always apply.
4. Investigate the Online cross-border change now. If January also breaches the action level, retrain the model on data that includes October-December.
5. Before changing any illustrative threshold, test the change against 2025 outcomes.

*Limitations: synthetic customer data and simulated analyst outcomes; documents and registers available only as a 31 December 2025 snapshot; thresholds are illustrative.*
