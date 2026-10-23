# govt-notice-telegram-alerts (v4 — 9 Verified Portals)

Production Python 3.12 and GitHub Actions monitor for **9 verified official Indian government recruitment, examination, and public-service commission websites**. Detects genuinely new official notices and sends one Telegram message per new notice while maintaining a Git-tracked JSON state file (`data/notice_state.json`).

---

## 1. Monitored Official Websites (9 Active Targets — 100% Operational)

| # | Source ID | Organization | Primary URL | Official Notice / API Endpoints | Adapter | Live Runner Verification Status |
|---|---|---|---|---|---|---|
| 1 | `ssc` | Staff Selection Commission (SSC) | `https://ssc.gov.in/` | `https://ssc.gov.in/api/general-website/portal/notice-boards` | `adapter_ssc` | **LIVE VERIFIED** — Queries SSC JSON API with attachment path resolution (**100 notices in baseline**) |
| 2 | `upsc` | Union Public Service Commission (UPSC) | `https://www.upsc.gov.in/` | `/whats-new`, `/examinations/active-exams`, `/recruitment/...` | `adapter_upsc` | **LIVE VERIFIED** — Parses UPSC What's New & Active Exam feeds (**57 notices in baseline**) |
| 3 | `uppsc` | Uttar Pradesh Public Service Commission (UPPSC) | `https://uppsc.up.nic.in/` | `/CandidatePages/Notifications.aspx`, `/` | `adapter_uppsc` | **LIVE VERIFIED** — Parses UPPSC Notifications & `Open_PDF_DB.aspx` rows (**50 notices in baseline**) |
| 4 | `hssc` | Haryana Staff Selection Commission (HSSC) | `https://www.hssc.gov.in/` | `/PublicNotices`, `/Advertisements`, `/Result` | `adapter_hssc` | **LIVE VERIFIED** — Parses HSSC chronological notice tables with row-context extraction (**41 notices in baseline**) |
| 5 | `rssb` | Rajasthan Staff Selection Board (RSSB) | `https://rssb.rajasthan.gov.in/` | `https://recruitment.rajasthan.gov.in/`, `/news`, `/advertisements` | `adapter_rssb` | **LIVE VERIFIED** — Official Rajasthan State Recruitment Portal + legacy TLS renegotiation (**7 notices in baseline**) |
| 6 | `mpesb` | Madhya Pradesh Employees Selection Board (MPESB) | `https://esb.mp.gov.in/` | `/e_default.html`, `https://esb.mponline.gov.in/` | `adapter_mpesb` | **VERIFIED ADAPTER** — Parses MPESB Latest Updates and MPOnline vacancy notices |
| 7 | `dsssb` | Delhi Subordinate Services Selection Board (DSSSB) | `https://dsssb.delhi.gov.in/` | `https://dsssbonline.nic.in/`, `/High-Light`, `/latest-vacancies` | `adapter_dsssb` | **LIVE VERIFIED** — Official OARS portal (`dsssbonline.nic.in`) active Post Code vacancies & eAdmit Cards (**9 notices in baseline**) |
| 8 | `upprpb` | Uttar Pradesh Police Recruitment and Promotion Board (UPPRPB) | `https://uppbpb.gov.in/` | `/Home/Notice`, `/Home/DirectRecruitment`, `/` | `adapter_upprpb` | **LIVE VERIFIED** — Parses UPPRPB notice board and official exam links (**38 notices in baseline**) |
| 9 | `ukpsc` | Uttarakhand Public Service Commission (UKPSC) | `https://psc.uk.gov.in/` | `https://ukpsc.net.in/`, `/public/announcement`, `/candidate-corner/recruitment` | `adapter_ukpsc` | **LIVE VERIFIED** — Official UKPSC examination portal (`ukpsc.net.in`) React bundle parser with SPA `#/` preservation (**8 notices in baseline**) |

---

## 2. Excluded & Pruned Portals

- **Excluded in 9-Site Configuration (`crpf`, `cisf`, `ssb`):** Central Reserve Police Force (`https://rect.crpf.gov.in/`), Central Industrial Security Force (`https://cisfrectt.cisf.gov.in/`), and Sashastra Seema Bal (`https://ssb.gov.in/`) have been removed from active monitoring while preserving all retained websites' state history and duplicate detection.
- **Inaccessible Portals Pruned Earlier (`upsssc`, `bpsc`, `bssc`, `hppsc`, `bsf`, `itbp`, `assam_rifles`):**

| Source ID | Organization | Primary URL | Root-Cause Analysis |
|---|---|---|---|
| `upsssc` | UPSSSC | `https://upsssc.gov.in/` | UP State Data Center (`164.100.181.233`) drops TCP SYN packets on ports 80/443 from non-Indian cloud IPs (Connection timeout). |
| `bpsc` | BPSC | `https://bpsc.bihar.gov.in/` | NIC Bihar perimeter firewall (`164.100.251.14` & `164.100.130.21`) sends immediate TCP `RST` (`errno 104`) to non-Indian cloud IPs (Connection reset). |
| `bssc` | BSSC | `https://bssc.bihar.gov.in/` | NIC Bihar firewall (`164.100.137.211`) sends immediate TCP `RST` (`errno 104`) to non-Indian cloud IPs (Connection reset). |
| `hppsc` | HPPSC | `https://hppsc.hp.gov.in/` | Authoritative `.hp.gov.in` DNS servers return `SERVFAIL` on bare domain queries from public resolvers and port 443 drops packets from cloud IPs. |
| `bsf` | BSF | `https://rectt.bsf.gov.in/` | NIC WAF returns `403 Forbidden` / connection timeout on cloud IPs; `bsf.mha.gov.in` is NXDOMAIN. |
| `itbp` | ITBP | `https://recruitment.itbpolice.nic.in/` | NIC servers (`164.100.158.135` & `164.100.58.234`) drop TCP SYN packets on port 443 from cloud IPs (Connection timeout). |
| `assam_rifles` | Assam Rifles | `https://www.assamrifles.gov.in/` | Server (`164.100.149.188`) drops TCP SYN packets on ports 80/443 from non-Indian cloud IPs (Connection timeout). |

---

## 3. Strict Keyword Filtering, Mandatory Publication-Date Validation & Duplicate Prevention

### 3.1 Strict Keyword Filtering (5 Official Categories)
Production alerts are sent **only** when the notice title or available row content matches at least one keyword (with word boundaries and case/punctuation/spacing normalization) from these 5 configurable categories:

- **Recruitment:** `new vacancy`, `recruitment`, `notification`, `advertisement`, `apply online`, `application form`, `posts`
- **Examination:** `exam date`, `examination date`, `exam schedule`, `admit card`, `hall ticket`, `city intimation`
- **Results:** `result`, `scorecard`, `merit list`, `cut off`, `cutoff`, `selection list`
- **Answer Keys:** `answer key`, `answerkey`, `provisional answer key`, `final answer key`, `response sheet`
- **Next Stages:** `DV`, `document verification`, `skill test`, `typing test`, `interview`, `PET`, `PST`, `medical examination`

**Contextual False-Positive Protection:**
- Automatically rejects tenders (`NIT`, `e-Tender`, `RFP`, `RFQ`, `GeM bid`), auctions, vendor/hospital empanelments, staff transfer/posting orders, seniority/gradation lists, pension adalats, holiday notifications, and annual reports.
- Validates ambiguous words (`posts`, `notification`, `DV`, `interview`, `PET`, `PST`) so unrelated phrases (such as *"sent by Speed Post / Registered Post"*, *"Ex-Post Facto"*, *"Holiday / Tariff Notification"*, *"Disbursement Voucher (DV No.)"*, or *"Media Interview"*) never trigger job alerts.

### 3.2 Mandatory Publication-Date Validation
Every production Telegram alert includes the **Official Publication Date**, **Organization**, **Notice Title**, **Category**, **Official Notice URL**, and **Application / Examination Dates** when available:
- **Freshness Limit:** Rejects notices older than `MAX_NOTICE_AGE_DAYS` (default: `30` days, configurable via `--max-age-days` or `MAX_NOTICE_AGE_DAYS`).
- **Strict Date Separation:** Never treats a page modified date (`Last modified on ...`), application deadline (`Last date to apply ...`, `Closing date ...`, `Visible upto ...`), examination date (`Exam Date: ...`), or scraping timestamp as the official publication date.
- **Missing or Invalid Dates:** If a notice's publication date is missing, invalid/impossible (e.g., `31-02-2026`), or in the future, the production alert is suppressed, logged for review, and recorded in state as `baselined` so it does not spam on future runs.
- **Historical & First-Run Safety:** Never sends historical notices during first runs, recovery runs, or baseline initialization.

### 3.3 Strong Cross-Page Duplicate Prevention & Delivery Guarantees
- **Canonical URL & Title Deduplication:** Deduplicates notices across multiple pages, URL query parameters, `http`/`https` schemes, `www` subdomains, `/whats-new` wrapper pages vs. direct PDF links, table serial-number shifts (`1.`, `S.No. 14`), file-size badges (`(1.5 MB)`), and `[NEW]` tags.
- **Confirmed Delivery Only:** Marks a notice as `delivered` (`sent`) **only after Telegram confirms `ok: true`**. Failed deliveries remain `failed` for bounded retry without creating duplicates.
- **Per-Run Cap:** Enforces a maximum of **10 production notifications per run** (`MAX_PRODUCTION_ALERTS_PER_RUN = 10`, configurable via `--max-alerts-per-run` or `MAX_ALERTS_PER_RUN`), deferring any overflow to the next scheduled run.

---

## 4. GitHub Actions Workflow & Configuration

1. In your GitHub repository, navigate to **Settings → Secrets and variables → Actions → Repository secrets**.
2. Add your secrets:
   - `TELEGRAM_BOT_TOKEN`: Your Telegram Bot API token from `@BotFather`.
   - `TELEGRAM_CHAT_ID`: Your Telegram group, supergroup, or channel ID (e.g. `-100xxxxxxxxxx`).
3. Optional environment variables / CLI flags:
   - `MAX_NOTICE_AGE_DAYS` / `--max-age-days`: Freshness threshold in days (default: `30`).
   - `MAX_ALERTS_PER_RUN` / `--max-alerts-per-run`: Maximum production notifications sent per run (default: `10`).
   - `NOTICE_KEYWORDS_JSON` / `--keywords-file`: Custom JSON mapping of category names to keyword arrays.
   - `NOTICE_EXTRA_KEYWORDS`: Comma-separated extra recruitment keywords.
4. Under the **Actions** tab, the workflow runs on schedule every 10 minutes (`*/10 * * * *`) or via manual `workflow_dispatch` (`auto`, `monitor`, `baseline`, `live_test`).

