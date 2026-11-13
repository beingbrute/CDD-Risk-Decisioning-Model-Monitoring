"""
Generate synthetic Customer Due Diligence (CDD) data for a UK bank, January-December 2025.

All customers, owners, documents, activity and review outcomes are synthetic.
The country risk reference data is real: it reproduces the FATF lists that were in force
during 2025, with the date each version took effect.

Outputs in data/raw/:
  country_risk_history.csv  FATF status of each jurisdiction, by list version and date range
  cdd_rules.csv             the CDD rules used later, with their UK legal basis
  customers.csv             one row per customer (plus planted duplicates)
  company_shareholders.csv  complete register of direct shareholders of company customers;
                            beneficial owners are the shareholders holding more than 25%
  documents.csv             identity / address / incorporation documents
  monthly_activity.csv      one row per customer per month, Oct 2024-Dec 2025
  review_cases.csv          analyst review cases with the outcome (escalated or not)
  expected_issues.csv       answer key: every planted data-quality issue

Design points:
- UK high-risk third countries. Since the Money Laundering and Terrorist Financing
  (High-Risk Countries) (Amendment) Regulations 2024 (SI 2024/69), regulation 33(3)(a) of the
  Money Laundering Regulations 2017 defines a high-risk third country as one named on the
  FATF lists "High-Risk Jurisdictions subject to a Call for Action" or "Jurisdictions under
  Increased Monitoring", as they have effect from time to time. Country status is therefore
  looked up on the date of each review, not taken from today's list.
- Information cutoff: reviews happen during 2025; each review uses only activity from
  months completed before the review month (trigger alerts, rules, model features and the
  simulated outcome all follow this). Activity starts in October 2024 so January reviews
  have history. Documents and shareholder registers exist only as a 31 Dec 2025 snapshot.
- The escalation outcome is simulated separately from the CDD rules. It depends on customer
  attributes, activity and a hidden factor that no rule or model sees, so a model cannot
  score perfectly by re-learning a rule.
- From October 2025 the activity of Online customers shifts (more cross-border and cash
  activity), so drift monitoring has something real to detect.
"""
from pathlib import Path

import numpy as np
import pandas as pd

SEED = 42
N_CUSTOMERS = 5000
AS_OF = pd.Timestamp("2025-12-31")
MONTHS = pd.period_range("2025-01", "2025-12", freq="M")              # review months
ACTIVITY_MONTHS = pd.period_range("2024-10", "2025-12", freq="M")     # 3 earlier months give history
DRIFT_RAMP = {"2025-10": 0.5, "2025-11": 0.8, "2025-12": 1.0}
HOME = "GB"

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "raw"

rng = np.random.default_rng(SEED)


def sigmoid(x):
    return 1 / (1 + np.exp(-x))


# =============================================================== 1. FATF reference data
# Each version took effect on the last day of the FATF plenary that published it.
FATF_VERSIONS = [
    # (version, publication / effective date, source)
    ("2024-10", "2024-10-25", "FATF plenary outcomes, 23-25 October 2024"),
    ("2025-02", "2025-02-21", "FATF plenary outcomes, 19-21 February 2025"),
    ("2025-06", "2025-06-13", "FATF plenary outcomes, 12-13 June 2025"),
    ("2025-10", "2025-10-24", "FATF plenary outcomes, 22-24 October 2025"),
]
CALL_FOR_ACTION = {"KP", "IR", "MM"}            # unchanged in all four versions
INCREASED_MONITORING = {
    "2024-10": {"DZ", "AO", "BG", "BF", "CM", "CI", "HR", "CD", "HT", "KE", "LB", "ML", "MC",
                "MZ", "NA", "NG", "PH", "ZA", "SS", "SY", "TZ", "VE", "VN", "YE"},
}
# Feb 2025: Philippines removed; Lao PDR and Nepal added
INCREASED_MONITORING["2025-02"] = (INCREASED_MONITORING["2024-10"] - {"PH"}) | {"LA", "NP"}
# Jun 2025: Croatia, Mali, Tanzania removed; Bolivia and Virgin Islands (UK) added
INCREASED_MONITORING["2025-06"] = (INCREASED_MONITORING["2025-02"] - {"HR", "ML", "TZ"}) | {"BO", "VG"}
# Oct 2025: Burkina Faso, Mozambique, Nigeria, South Africa removed; none added
INCREASED_MONITORING["2025-10"] = INCREASED_MONITORING["2025-06"] - {"BF", "MZ", "NG", "ZA"}

COUNTRY_NAMES = {
    "GB": "United Kingdom", "IE": "Ireland", "FR": "France", "DE": "Germany", "NL": "Netherlands",
    "ES": "Spain", "PL": "Poland", "RO": "Romania", "US": "United States",
    "AE": "United Arab Emirates", "IN": "India", "PK": "Pakistan", "CN": "China",
    "HK": "Hong Kong", "TR": "Turkey",
    "KP": "Democratic People's Republic of Korea", "IR": "Iran", "MM": "Myanmar",
    "DZ": "Algeria", "AO": "Angola", "BO": "Bolivia", "BG": "Bulgaria", "BF": "Burkina Faso",
    "CM": "Cameroon", "CI": "Cote d'Ivoire", "HR": "Croatia",
    "CD": "Democratic Republic of the Congo", "HT": "Haiti", "KE": "Kenya", "LA": "Lao PDR",
    "LB": "Lebanon", "ML": "Mali", "MC": "Monaco", "MZ": "Mozambique", "NA": "Namibia",
    "NP": "Nepal", "NG": "Nigeria", "PH": "Philippines", "ZA": "South Africa",
    "SS": "South Sudan", "SY": "Syria", "TZ": "Tanzania", "VE": "Venezuela", "VN": "Vietnam",
    "VG": "Virgin Islands (UK)", "YE": "Yemen",
}

hist_rows = []
for k, (ver, start, source) in enumerate(FATF_VERSIONS):
    end = (pd.Timestamp(FATF_VERSIONS[k + 1][1]) - pd.Timedelta(days=1)).date() if k + 1 < len(FATF_VERSIONS) else None
    for code, name in COUNTRY_NAMES.items():
        if code in CALL_FOR_ACTION:
            status = "Call for action"
        elif code in INCREASED_MONITORING[ver]:
            status = "Increased monitoring"
        else:
            status = "Not listed"
        hist_rows.append({
            "country_code": code, "country_name": name, "fatf_status": status,
            "uk_high_risk_third_country": int(status != "Not listed"),
            "list_version": ver, "effective_from": start, "effective_to": end, "source": source,
        })
country_hist = pd.DataFrame(hist_rows)
VERSION_STARTS = pd.to_datetime([v[1] for v in FATF_VERSIONS])
STATUS_LEVEL = {"Not listed": 0, "Increased monitoring": 1, "Call for action": 2}
LEVEL = {(r.country_code, r.list_version): STATUS_LEVEL[r.fatf_status] for r in country_hist.itertuples()}


def version_on(date):
    """FATF list version in force on a date (data starts after the first version)."""
    idx = VERSION_STARTS.searchsorted(pd.Timestamp(date), side="right") - 1
    return FATF_VERSIONS[idx][0]


def country_level(code, date):
    return LEVEL.get((code, version_on(date)), 0)


# =============================================================== 2. CDD rules (reference)
# legal_basis = the UK provision the rule relates to. basis_type says what is required by law
# and what is a bank policy choice or an illustrative threshold (banks set these themselves).
MLR = "Money Laundering Regulations 2017"
cdd_rules = pd.DataFrame([
    ("R01", "Customer established in a UK high-risk third country on the review date",
     "Enhanced due diligence", f"{MLR} reg. 33(1)(b), 33(3)(a) as amended by SI 2024/69",
     "Required by regulation"),
    ("R02", "Beneficial owner (more than 25%) established in a FATF-listed jurisdiction",
     "Geographical risk assessment; apply EDD if the relationship is assessed as high risk",
     f"{MLR} reg. 28(3A), 33(1)(a), 33(6)(c)", "Risk factor; outcome is a bank judgement"),
    ("R03", "Customer or beneficial owner is a PEP",
     "EDD, senior management approval, source of wealth and funds, enhanced ongoing monitoring",
     f"{MLR} reg. 33(1)(d), 35(5)(a)-(c); domestic PEPs lower starting risk, reg. 35(3A)",
     "Required by regulation"),
    ("R04", "Unresolved potential sanctions screening match",
     "Refer to sanctions team before any further dealing",
     "UK financial sanctions regimes under the Sanctions and Anti-Money Laundering Act 2018",
     "Legal prohibition on dealing with designated persons; referral process is bank policy"),
    ("R05", "Adverse media on the customer",
     "Risk review", f"{MLR} reg. 33(1)(a), 33(1)(g)", "Bank policy"),
    ("R06", "Monthly activity more than 2x declared expected turnover",
     "Examine background and purpose; request source of funds",
     f"{MLR} reg. 33(1)(f), 33(4), 28(11)(a)", "Illustrative threshold (2x)"),
    ("R07", "Cash share of activity above 30%",
     "Activity review", f"{MLR} reg. 33(6)(a)(v) (cash-intensive business)", "Illustrative threshold (30%)"),
    ("R08", "Share of flows to or from FATF-listed jurisdictions above 10%",
     "Activity review", f"{MLR} reg. 33(6)(c), 28(11)(a)", "Illustrative threshold (10%)"),
    ("R09", "Two or more transaction-monitoring alerts in the month",
     "Trigger review", f"{MLR} reg. 28(11)(a)", "Illustrative threshold (2 alerts)"),
    ("R10", "Identity, address or incorporation document missing, pending or expired",
     "Refresh CDD documents", f"{MLR} reg. 28(2)(a)-(b), 28(3), 28(11)(b)",
     "Bank policy applying the duty to keep CDD up to date"),
    ("R11", "Company with no shareholder register, or a beneficial owner (more than 25%) not verified",
     "Identify and verify beneficial owners", f"{MLR} reg. 5(1)(b), 28(3A), 28(4)",
     "Required by regulation (more than 25%)"),
    ("R12", "Periodic review overdue (High 1 year, Medium 2 years, Low 3 years)",
     "Schedule review", f"{MLR} reg. 28(11)(b)", "Illustrative review cycle"),
], columns=["rule_code", "description", "next_action", "legal_basis", "basis_type"])

# =============================================================== 3. customers
RES_P = {"GB": .80, "IE": .02, "PL": .02, "FR": .015, "DE": .015, "RO": .015, "US": .015,
         "IN": .015, "NL": .01, "ES": .01, "AE": .01, "PK": .01, "NG": .008, "ZA": .006,
         "CN": .005, "HK": .005, "TR": .005, "KE": .004, "BG": .004, "HR": .004, "PH": .004,
         "VN": .003, "VG": .003, "MC": .002, "NP": .002, "LB": .002}
OWN_P = {"GB": .70, "VG": .04, "AE": .03, "IE": .02, "US": .02, "CN": .02, "HK": .02, "IN": .02,
         "FR": .015, "DE": .015, "PL": .01, "NG": .01, "ZA": .01, "TR": .01, "PK": .01, "MC": .008,
         "KE": .005, "BG": .005, "HR": .005, "PH": .005, "VN": .005, "LB": .005, "IR": .002}


def pick_country(probs, n):
    codes = list(probs)
    p = np.array(list(probs.values()))
    return rng.choice(codes, size=n, p=p / p.sum())


ids = [f"CUST{i:05d}" for i in range(1, N_CUSTOMERS + 1)]
is_company = rng.random(N_CUSTOMERS) < 0.25

IND_OCC = {"Salaried": 0, "Retired": 0, "Student": 0, "Self-employed": 1, "Business owner": 1}
COMP_IND = {"Retail": 0, "Professional services": 0, "Manufacturing": 0, "Construction": 1,
            "Real estate": 1, "Hospitality": 1, "Money services": 2, "Car dealership": 2,
            "Crypto services": 2}
CASH_INTENSIVE = {"Hospitality", "Car dealership", "Money services"}

occ = np.where(
    is_company,
    rng.choice(list(COMP_IND), N_CUSTOMERS, p=[.2, .2, .15, .12, .1, .1, .05, .05, .03]),
    rng.choice(list(IND_OCC), N_CUSTOMERS, p=[.55, .15, .1, .12, .08]),
)
or_num = np.array([COMP_IND.get(o, IND_OCC.get(o)) for o in occ])
cash_intensive = np.isin(occ, list(CASH_INTENSIVE))

country = pick_country(RES_P, N_CUSTOMERS)
pep = rng.random(N_CUSTOMERS) < 0.015
domestic_pep = pep & (country == HOME)
adverse_media = rng.random(N_CUSTOMERS) < 0.03
screening_hit = rng.random(N_CUSTOMERS) < 0.02            # potential sanctions name match
channel = rng.choice(["Branch", "Online", "Intermediary"], N_CUSTOMERS, p=[.35, .5, .15])

onboard = pd.to_datetime("2015-01-01") + pd.to_timedelta(
    rng.integers(0, (pd.Timestamp("2025-12-01") - pd.Timestamp("2015-01-01")).days, N_CUSTOMERS), unit="D")

turnover = np.where(is_company,
                    rng.lognormal(10.3, 0.9, N_CUSTOMERS),      # ~GBP 30k median
                    rng.lognormal(7.9, 0.6, N_CUSTOMERS))       # ~GBP 2.7k median
turnover = np.round(turnover, -1)

# ---- shareholder register (needed before the rating). The register is complete, so the
# shares of each company add up to 100%; beneficial owners are holders of more than 25%.
bo_rows = []
for cid, comp in zip(ids, is_company):
    if not comp:
        continue
    k = rng.choice([1, 2, 3, 4], p=[.35, .35, .2, .1])
    cuts = np.sort(rng.choice(np.arange(5, 96), k - 1, replace=False)) if k > 1 else []
    shares = np.diff(np.concatenate([[0], cuts, [100]]))
    for j, s in enumerate(shares, 1):
        bo_rows.append({
            "customer_id": cid, "shareholder_id": f"{cid}-S{j}", "ownership_pct": float(s),
            "shareholder_country_code": pick_country(OWN_P, 1)[0],
            "shareholder_pep_flag": int(rng.random() < 0.02),
            "verified": int(rng.random() < 0.93),
        })
owners = pd.DataFrame(bo_rows)
cust_pos = {c: i for i, c in enumerate(ids)}
bos = owners[owners.ownership_pct > 25]                    # beneficial owners, reg. 5(1)(b)
owner_pep = np.zeros(N_CUSTOMERS, bool)
owner_pep[[cust_pos[c] for c in bos.customer_id[bos.shareholder_pep_flag == 1]]] = True


def owner_level(date):
    """Highest FATF level among a company's beneficial owners on a date."""
    lv = np.zeros(N_CUSTOMERS, int)
    for r in bos.itertuples():
        lv[cust_pos[r.customer_id]] = max(lv[cust_pos[r.customer_id]], country_level(r.shareholder_country_code, date))
    return lv


# ---- the bank's risk rating, as re-assessed against the list in force on 31 Dec 2025
cl_asof = np.array([country_level(c, AS_OF) for c in country])
ol_asof = owner_level(AS_OF)
pts = (3 * cl_asof + 2 * (ol_asof > 0) + or_num + is_company.astype(int)
       + 2 * adverse_media + cash_intensive + (channel == "Intermediary"))
rating = np.where(pts >= 4, "High", np.where(pts >= 2, "Medium", "Low"))
# Internal rating policy (illustrative): non-domestic PEPs High; domestic PEPs at least
# Medium, reflecting their lower starting risk under reg. 35(3A)
rating = np.where(pep & ~domestic_pep, "High", rating)
rating = np.where(domestic_pep & (rating == "Low"), "Medium", rating)

REVIEW_YEARS = {"High": 1, "Medium": 2, "Low": 3}
freq = pd.Series(rating).map(REVIEW_YEARS).to_numpy()
days_since_onboard = (AS_OF - onboard).days.to_numpy()
last_rev_offset = rng.integers(0, np.maximum(days_since_onboard, 1)) % (freq * 365 + 200)
last_review = AS_OF - pd.to_timedelta(np.minimum(last_rev_offset, days_since_onboard), unit="D")
last_review = pd.Series(last_review).where(pd.Series(last_review) >= pd.Series(onboard), pd.Series(onboard))
next_due = last_review + pd.to_timedelta(freq * 365, unit="D")

customers = pd.DataFrame({
    "customer_id": ids,
    "customer_type": np.where(is_company, "Company", "Individual"),
    "onboard_date": onboard.date,
    "country_code": country,
    "occupation_or_industry": occ,
    "channel": channel,
    "pep_flag": pep.astype(int),
    "adverse_media_flag": adverse_media.astype(int),
    "screening_hit_flag": screening_hit.astype(int),
    "expected_monthly_turnover": turnover,
    "risk_rating": rating,
    "last_review_date": last_review.dt.date,
    "next_review_due": next_due.dt.date,
})

# Hidden factor no rule or model sees: genuine concern about the customer
hidden = rng.normal(0, 1, N_CUSTOMERS)
true_match = screening_hit & (rng.random(N_CUSTOMERS) < 0.15)     # most hits are false positives

# =============================================================== 4. documents
doc_rows = []
for cid, comp in zip(ids, is_company):
    types = ["Certificate of incorporation", "Proof of address"] if comp else ["Photo ID", "Proof of address"]
    for t in types:
        issue = AS_OF - pd.Timedelta(days=int(rng.integers(30, 3650)))
        expiry = issue + pd.Timedelta(days=3650) if t == "Photo ID" else pd.NaT
        status = rng.choice(["Verified", "Pending", "Missing"], p=[.92, .05, .03])
        doc_rows.append({"customer_id": cid, "doc_type": t, "status": status,
                         "issue_date": issue.date(), "expiry_date": expiry.date() if pd.notna(expiry) else None})
documents = pd.DataFrame(doc_rows)
doc_flag = np.isin(ids, documents[documents.status != "Verified"].customer_id.unique())

# =============================================================== 5. monthly activity
cash_base = np.clip(rng.beta(1.5, 12, N_CUSTOMERS) + 0.08 * or_num + 0.10 * cash_intensive, 0, 1)
xb_base = np.clip(rng.beta(1.2, 15, N_CUSTOMERS) + 0.10 * (country != HOME) + 0.08 * (ol_asof > 0), 0, 1)
behaviour = 0.35 * hidden + rng.normal(0, 0.25, N_CUSTOMERS)     # hidden concern shows a little

act_rows = []
onboard_period = pd.PeriodIndex(onboard, freq="M")
for m in ACTIVITY_MONTHS:
    active = onboard_period <= m
    ramp = DRIFT_RAMP.get(str(m), 0.0)
    drift = ramp * (channel == "Online") * np.where(onboard_period >= pd.Period("2025-01", freq="M"), 2.0, 1.0)
    n = N_CUSTOMERS
    ratio = np.exp(rng.normal(0, 0.35, n) + np.clip(behaviour, -1, 2) * 0.6 + drift * 0.25)
    value = np.round(turnover * ratio, 2)
    cnt = rng.poisson(np.where(is_company, 60, 18) * np.sqrt(ratio))
    cash = np.clip(cash_base + rng.normal(0, 0.03, n) + drift * 0.05, 0, 1)
    xb = np.clip(xb_base + rng.normal(0, 0.03, n) + drift * 0.09, 0, 1)
    mid = m.start_time + pd.Timedelta(days=14)            # FATF status in force mid-month
    cl_m = np.array([country_level(c, mid) for c in country])
    hr = np.clip(xb * (0.1 + 0.4 * (cl_m > 0)) + np.clip(behaviour, 0, None) * 0.05, 0, 1)
    alerts = rng.poisson(0.05 + 0.4 * np.clip(behaviour, 0, None) + 0.3 * (ratio > 2))
    for i in np.where(active)[0]:
        act_rows.append((ids[i], str(m), int(cnt[i]), float(value[i]), round(float(cash[i]), 4),
                         round(float(xb[i]), 4), round(float(hr[i]), 4), int(alerts[i])))
activity = pd.DataFrame(act_rows, columns=[
    "customer_id", "month", "txn_count", "txn_value", "cash_share",
    "cross_border_share", "high_risk_country_share", "tm_alerts"])

# =============================================================== 6. review cases + outcome
act_idx = activity.set_index(["customer_id", "month"])
owner_level_by_version = {v[0]: owner_level(v[1]) for v in FATF_VERSIONS}
case_rows = []
case_no = 0
# Information cutoff: a review in month m happens on a date inside month m, so only months
# completed before it are known. Trigger reviews come from the previous month's monitoring
# alerts, and the analyst's decision (the outcome) depends on the previous month's activity,
# never on activity after the review date.
for m in MONTHS:
    prev = m - 1
    month_act = activity[activity.month == str(prev)]
    trig = month_act[month_act.tm_alerts > 0].customer_id
    trig = set(trig[rng.random(len(trig)) < 0.6])
    periodic = set(month_act.customer_id.sample(frac=0.04, random_state=int(rng.integers(1e9))))
    for cid in sorted(trig | periodic):
        i = cust_pos[cid]
        review_date = m.start_time + pd.Timedelta(days=int(rng.integers(0, m.days_in_month)))
        ver = version_on(review_date)
        cl = LEVEL.get((country[i], ver), 0)                       # status on the review date
        ol = owner_level_by_version[ver][i]
        a = act_idx.loc[(cid, str(prev))]                         # last completed month
        r = a.txn_value / turnover[i]
        logit = (-3.5
                 + 1.0 * cl + 0.5 * ol + 0.5 * or_num[i]
                 + 1.0 * (pep[i] & ~domestic_pep[i]) + 0.5 * domestic_pep[i] + 0.4 * owner_pep[i]
                 + 0.8 * adverse_media[i] + 3.0 * true_match[i]
                 + 0.9 * np.log(max(r, 0.1)) + 2.5 * max(a.cash_share - 0.25, 0)
                 + 2.0 * a.high_risk_country_share + 0.35 * min(a.tm_alerts, 4)
                 + 0.5 * doc_flag[i]
                 + 1.1 * hidden[i])                                # unobserved
        case_no += 1
        case_rows.append({
            "case_id": f"CASE{case_no:06d}", "customer_id": cid,
            "review_date": review_date.date(), "review_month": str(m),
            "review_type": "Trigger" if cid in trig else "Periodic",
            "analyst_escalated": int(rng.random() < sigmoid(logit)),
        })
cases = pd.DataFrame(case_rows)

# =============================================================== 7. planted data-quality issues
issues, used = [], set()


def pick(pool, k):
    pool = [p for p in pool if p not in used]
    sel = list(rng.choice(pool, k, replace=False))
    used.update(sel)
    return sel


def log(check, table, keys):
    issues.extend({"check": check, "table": table, "key": k} for k in keys)


comp_ids = [c for c, comp in zip(ids, is_company) if comp]
non_pep = customers.customer_id[customers.pep_flag == 0].tolist()
low_non_pep = customers.customer_id[(customers.risk_rating == "Low") & (customers.pep_flag == 0)].tolist()

sel = pick(ids, 8)
customers.loc[customers.customer_id.isin(sel), "country_code"] = ""
log("missing_country_code", "customers", sel)

sel = pick(non_pep, 6)
for c, b in zip(sel, ["Hgh", "medium", "LOW ", "Unknown", "Med", "N/A"]):
    customers.loc[customers.customer_id == c, "risk_rating"] = b
log("invalid_risk_rating", "customers", sel)

# PEP rated below the bank's internal rating policy. This is a policy-consistency exception,
# not in itself a regulatory breach: the regulations require EDD for PEPs (reg. 33(1)(d)),
# not a particular rating label. Low is below policy for domestic and non-domestic PEPs.
sel = pick(low_non_pep, 5)
customers.loc[customers.customer_id.isin(sel), "pep_flag"] = 1
log("pep_rating_policy_exception", "customers", sel)

sel = pick(ids, 4)
for c in sel:
    row = customers.customer_id == c
    yrs = REVIEW_YEARS[customers.loc[row, "risk_rating"].iloc[0]]
    customers.loc[row, "last_review_date"] = (AS_OF + pd.Timedelta(days=45)).date()
    customers.loc[row, "next_review_due"] = (AS_OF + pd.Timedelta(days=45 + yrs * 365)).date()
log("review_date_in_future", "customers", sel)

sel = pick(ids, 6)
for c in sel:
    row = customers.customer_id == c
    lr = pd.Timestamp(customers.loc[row, "last_review_date"].iloc[0])
    customers.loc[row, "next_review_due"] = (lr + pd.Timedelta(days=5 * 365)).date()
log("review_cycle_mismatch", "customers", sel)

sel = pick(comp_ids, 7)
for c in sel:
    owners.loc[owners.index[owners.customer_id == c][0], "ownership_pct"] += 15
log("register_not_100", "company_shareholders", sel)

sel = pick(comp_ids, 5)
owners = owners[~owners.customer_id.isin(sel)]
log("company_without_register", "customers", sel)

ok_ids = documents.customer_id[(documents.doc_type == "Photo ID") & (documents.status == "Verified")]
sel = pick(ok_ids.unique().tolist(), 10)
for c in sel:
    row = (documents.customer_id == c) & (documents.doc_type == "Photo ID")
    documents.loc[row, "expiry_date"] = (AS_OF - pd.Timedelta(days=int(rng.integers(10, 400)))).date()
log("verified_doc_expired", "documents", sel)

sel = pick(ids, 5)
for c in sel:
    row = activity.index[activity.customer_id == c][-1]
    activity.loc[row, "txn_value"] = -abs(activity.loc[row, "txn_value"])
log("negative_txn_value", "monthly_activity", sel)

orphans = [f"CUST{9000 + i:05d}" for i in range(6)]
extra = activity.sample(6, random_state=SEED).copy()
extra["customer_id"] = orphans
activity = pd.concat([activity, extra], ignore_index=True)
log("activity_orphan_customer", "monthly_activity", orphans)

sel = pick(ids, 5)
customers = pd.concat([customers, customers[customers.customer_id.isin(sel)]], ignore_index=True)
log("duplicate_customer_id", "customers", sel)

# =============================================================== 8. write
OUT.mkdir(parents=True, exist_ok=True)
for old in ["country_risk.csv", "beneficial_owners.csv"]:                     # file from an earlier version
    (OUT / old).unlink(missing_ok=True)
country_hist.to_csv(OUT / "country_risk_history.csv", index=False)
cdd_rules.to_csv(OUT / "cdd_rules.csv", index=False)
customers.to_csv(OUT / "customers.csv", index=False)
owners.to_csv(OUT / "company_shareholders.csv", index=False)
documents.to_csv(OUT / "documents.csv", index=False)
activity.to_csv(OUT / "monthly_activity.csv", index=False)
cases.to_csv(OUT / "review_cases.csv", index=False)
pd.DataFrame(issues).to_csv(OUT / "expected_issues.csv", index=False)

print(f"country_risk_history.csv {len(country_hist):>6} rows ({country_hist.country_code.nunique()} jurisdictions x {len(FATF_VERSIONS)} FATF list versions)")
print(f"cdd_rules.csv            {len(cdd_rules):>6} rules")
print(f"customers.csv            {len(customers):>6} rows ({customers.customer_id.nunique()} unique)")
print(f"company_shareholders.csv {len(owners):>6} rows")
print(f"documents.csv            {len(documents):>6} rows")
print(f"monthly_activity.csv     {len(activity):>6} rows")
print(f"review_cases.csv         {len(cases):>6} rows, escalation rate {cases.analyst_escalated.mean():.1%}")
print(f"expected_issues.csv      {len(issues):>6} planted issues")
print(pd.DataFrame(issues).groupby("check").size().to_string())
