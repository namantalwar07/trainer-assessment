# Trainer Assessment System

Scores recorded training sessions against a 10-competency, 100-point rubric with `gemini-3.8-flash`.
Per recording it produces a validated scorecard with timestamped evidence, a PDF report, rows in a 7-sheet
Excel workbook, and a full audit trail. **For trainer development only. Nothing here triggers any
disciplinary action, and scores should not be used for such decisions until the pilot passes.**

---

## Start here: first run (about 10 minutes)

**Already done on this Mac:** virtual environment with all dependencies (including the Gemini and GCS SDKs),
FFmpeg 9.0.2, and a `.env` with a generated `SESSION_SECRET`. On a new machine run `make setup-live` and
`make init` first (see "Python notes" if setup fails).

```bash
cd trainer-assessment
```

1. Open `.env` and paste your key: `GEMINI_API_KEY=...` (paid-tier project; never share or commit `.env`).
2. Create your admin account (prompts for a password, 12+ characters, letters and digits):
   ```bash
   make create-admin
   ```
3. Run the pre-flight check. It must end with "All blocking checks passed":
   ```bash
   make doctor
   ```
   It verifies FFmpeg, the database, admin user, disk space, that your key can see `gemini-3.8-flash`, and
   makes one tiny real API call.
4. Start the app and open http://127.0.0.1:8000
   ```bash
   make run
   ```
5. In the browser: **Modules** (add each module's reference content) → **Users** (add operators/reviewers)
   → **Upload** (drag in recordings).

Before uploading a real recording, upload one short **approved sample** first and check the result end to end.

## Daily use

| Role | What to do |
|---|---|
| Operator | **Upload**: drop files, each gets its own progress bar and language. **Sessions**: watch status; press Retry on failures. |
| Reviewer | **Sessions → Open**: scorecard, timestamped evidence, PDF, video, human review form, coaching tracker. **Excel export** gives the master workbook. |
| Admin | Users, Modules (reference content for C01), Trainers (names), delete sessions. |

File name format (strict): `STATE_MODULE_TRAINERID_SESSIONID_DATE.mp4`, for example
`UP_BUSINESSFUNDAMENTALS_TR001_S001_20261104.mp4`. The browser rejects bad names before uploading, and the
server checks again. MP4 or MOV, up to 1 GB (set `MAX_UPLOAD_BYTES` to change). A 400 MB recording uploads
in seconds on a local network; the server streams it to disk, so memory stays flat (measured: 403 MB upload,
132 MB peak server memory).

**Sessions needing human review** are flagged by backend rules, never by the model: any critical flag,
coverage below 70 rubric points, C01 N/A because the module has no reference content, Low confidence on a
competency weighing 10+, or model output that needed validation retries.

## What happens to each recording

1. **Upload checks**: extension, real MP4/MOV signature, size, duplicate file (SHA-256), duplicate session,
   free disk space. Storage keys are generated server-side; the file name is never used as a path.
2. **FFmpeg validation**: container, codecs, audio track, duration (warns outside 60–90 min, 45–120),
   decode check of the first and last 20 seconds. Unreadable files are **quarantined** with a plain-language reason.
3. **Stage 1 (video)**: Files API upload, then one agentic Interactions call (`background=True`) that returns a
   timestamped transcript, interaction events and visual observations. If the Interactions call fails for a
   non-transient reason, it falls back to a plain `generate_content` call on the same file
   (`EXTRACTION_FALLBACK=false` disables this). The path actually used is stored per assessment.
4. **Stage 2 (text only)**: scoring prompt + rubric + JSON Schema. Validated against the schema and the
   backend rules (all 10 criteria once, weights, rating/N/A consistency, evidence for every rated competency,
   timestamps inside the real duration). Up to 3 retries with exponential backoff; retries cost only text
   tokens. If it still fails, raw outputs are kept and the job shows as Failed (Retry button).
5. **Backend calculation**: weighted points (`rating/5 × weight`), proportional N/A redistribution, overall
   score, band, coverage line and review decision. Model-supplied totals and flags are ignored.
6. **Outputs**: PDF report, database rows, workbook regeneration, and the Files API object is deleted.

Every assessment records model, prompt version, rubric version and API path; every job records token usage and
an estimated cost in ₹ (`USD_INR`, prices configurable; re-verify against the published price list).

## The Session Recording Review (primary report)

The PDF follows the review format used by the programme lead, in this order: **Session** (invoice row, date,
MSME, invoiced time, recording), **Result**, **Integrity**, **Gaps of 2.5 minutes or more** (clock time, length,
what was said before and after), **Invoiced length vs recording length**, **Observation**. Everything after the
page titled *Additional analysis* is extra (effective teaching time, mentees, conduct, trainer scorecard).

- **Invoice sheet:** open **Invoices**, import your sheet as CSV (flexible column names; a template can be
  downloaded) or add rows by hand. Recordings link automatically when exactly one row has the same trainer and
  date; otherwise type the invoice row when uploading, or link it later on the Invoices page (the PDF is rebuilt).
  Without a linked row the report still works and says no invoice was compared.
- **What is measured by software** (FFmpeg, no AI): silent stretches, frozen picture, jumps in the video's internal
  timestamps, editing-tool tags, audio/video length mismatch. A measured silence of 2.5 minutes or more is
  reported as a gap even if the AI called it teaching.
- **What the AI reads:** the meeting clock on screen (wall-clock times of the recording), what was said around each
  gap, whether the same mentor and participant appear throughout, signs of editing, why a freeze happened.
  Clock times in the report come from those readings. If no clock is visible, times are shown as recording time.
- **All numbers and sentences** (Result, comparison table, paragraph, Observation) are generated in code from
  those facts, so they are reproducible. The Observation adds one AI sentence about the likely cause only when the
  recording itself shows it (for example the participant mentions a Wi-Fi problem).
- Wording is factual and neutral ("No signs of tampering" means none were found; it is not a forensic
  certification). Any integrity issue, a gap, or a shortfall against the invoice sets *human review required*.

## Effective teaching time, mentee names and trainer conduct

Every recording also gets an **activity timeline** (what the trainer was doing, minute by minute). The backend
turns it into numbers, so the rules are exact and adjustable:

| Rule | Default | Setting |
|---|---|---|
| Minimum effective teaching time | 60 min | `MIN_EFFECTIVE_MINUTES` |
| Pause forgiven (counted as effective) | up to 2 min | `GAP_TOLERANCE_SECONDS` (120) |
| Pause flagged (the sir's "2.5 minutes or more") | 2.5 min or longer | `GAP_FLAG_SECONDS` (150) |
| Participant exercises with trainer present | count as effective | `COUNT_PARTICIPANT_ACTIVITY_AS_EFFECTIVE` |
| Technical problem (projector, audio, network) | not held against the trainer | review if over `TECHNICAL_REVIEW_MINUTES` (10) |

- Consecutive non-teaching spans (break, trainer absent, silence, off-task) form **one** pause. A 2 min 20 s pause
  is counted off but not flagged; anything of 2.5 min or more is flagged, shown in red in the PDF and web page, listed in
  the Excel sheet **Time and Conduct**, and routes the session to human review.
- Effective time below the minimum also routes to human review. None of this changes the competency score.
- **Mentee names** are collected only when actually spoken or shown (introductions, attendance, being addressed,
  on-screen labels), de-duplicated, with first sighting, source and spelling confidence. They are AI
  transcriptions: verify against the attendance sheet. Shown in the PDF, the web page and the **Participants** sheet.
- **Trainer conduct observations** are limited to observable behaviour (phone use, leaving the session area,
  unrelated conversation, not attending to participants, repeatedly out of view, eating/drinking). Moving around
  to help participants is not an event. The system never infers intent, attention or seriousness from faces; it
  lists what was seen so a human can judge. Three or more events, or one lasting over 3 min, route to review.
- Detection quality depends on what the camera and microphone capture. If the trainer is often off camera, absence
  will be hard to tell from teaching; calibrate on a few recordings you have checked by hand before trusting it.

## Outputs

- **PDF** (Sessions → Open → Download PDF): header, score banner, scorecard with rating bars, timestamped
  evidence, strengths, development areas, coaching plan, critical flags, coverage disclosure, versions footer.
  Reports are written in English (the PDF font set has no Devanagari glyphs); prompts request English output
  with short transliterated quotes for Hindi and regional-language sessions.
- **Excel** (top bar → Excel export; also regenerated automatically after each batch): Master Assessment,
  Competency Scores (long format for Power BI), Evidence Register, Trainer Summary, Language and Regional,
  Time and Conduct, Participants, Coaching Tracker, Management Dashboard (with charts). A **Session Review** sheet
  holds the review summary (Result and Observation text plus the numbers) for every recording. Trainers and language groups with fewer than 3 sessions
  are shown with counts only, never averaged or ranked.

## Getting the API key and the cost

See [docs/GEMINI_KEY_AND_COST.md](docs/GEMINI_KEY_AND_COST.md) for the key steps, verified pricing and INR estimates
(about Rs 21,000 for 450 recordings at default resolution, double after 1 Jan 2027).

## Settings (`.env`)

`SCORE_FLOOR_MODE` (accept | zero_anchor), `BAND_ROUNDING_RULE` (threshold | round_half_up),
`MIN_COVERAGE_POINTS` (70), `MIN_EFFECTIVE_MINUTES` (60), `GAP_TOLERANCE_SECONDS` (120), `GAP_FLAG_SECONDS` (150), `INVOICE_SHORTFALL_MINUTES` (5), `CLOCK_TOLERANCE_SECONDS` (120), `SILENCE_MIN_SECONDS` (20), `RUN_FREEZE_DETECT` (true), `LOW_CONF_WEIGHT_THRESHOLD` (10), `MIN_SESSIONS_FOR_AGGREGATE` (3),
`WORKER_CONCURRENCY` (2), `MAX_VALIDATION_RETRIES` (3), `VIDEO_RESOLUTION` (empty | low | medium | high),
`RETENTION_DAYS` (30), `DELETE_VIDEO_AFTER_ASSESSMENT`, `MIN_FREE_DISK_GB`, `USD_INR`, `PUBLIC_BASE_URL`.
The rubric is `config/rubric_v1.json`; prompts are versioned files in `prompts/`.

**Disk space:** 450 recordings at 0.4 GB is about 180 GB. Process in waves of 10–20, set
`DELETE_VIDEO_AFTER_ASSESSMENT=true` (scores, evidence and PDFs are kept), or use `STORAGE_BACKEND=gcs`.
Uploads are refused automatically when free space drops below `MIN_FREE_DISK_GB`.

## Company deployment

`docker-compose.yml` runs PostgreSQL + the app + Caddy (automatic HTTPS, 2 GB upload limit, security headers):

```bash
cp .env.example .env    # set GEMINI_API_KEY, SESSION_SECRET, POSTGRES_PASSWORD, SITE_ADDRESS
docker compose up -d --build
docker compose exec app python scripts/create_user.py you@company.com admin
```

The Docker files have not been run on this machine (Docker was not installed). Check them in staging first.
Also required before production: SSO or your password policy sign-off, HTTPS (`COOKIE_SECURE=true`), a private
GCS bucket if you use one (public access prevention on), daily `make backup` copied off the machine, daily
`make purge` (enforces `RETENTION_DAYS`), and a written consent notice and retention policy (India DPDP Act 2023).
Google deletes uploaded Files after 48 hours and we delete them explicitly after each job; that says nothing
about other provider-side logs, so state your own commitments in your documentation.

## Security summary

Per-user accounts (scrypt hashes, 12+ character passwords, lockout after 5 failed logins in 15 minutes);
roles enforced server-side on every route; CSRF protection on every form and API call; SameSite=Strict
cookies; videos and PDFs only through authenticated, role-checked routes (`Cache-Control: no-store`);
no public links, no inbound webhooks; every login, upload, view, score, export, review and delete is in
`audit_log`; the API key is read from the environment only and never logged. Mock mode (`make run-mock`)
uses dummy fixtures and shows a banner: never use it for real recordings.

## Operations

| Task | Command |
|---|---|
| Health check | `make doctor` |
| Run tests / lint | `make test`, `make lint` |
| Backup database + reports | `make backup` (keeps the newest 14) |
| Delete videos past retention | `make purge` |
| Logs | `data/logs/app.log` (rotating) and the audit trail in the database |
| Offline demo (dummy data) | `make seed-demo`, `make sample-videos`, `make run-mock` |

### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| App refuses to start: needs GEMINI_API_KEY | Add the key to `.env` (live mode fails loudly by design). |
| `make doctor`: model not in this key's model list | The key/project cannot see `gemini-3.8-flash`. Fix access; no other model is enabled. |
| Quarantined with "incomplete or corrupted" | Re-export or re-copy the file (often an interrupted copy). |
| Failed with a Gemini error | Press Retry. Quota/rate limits retry automatically; HTTP 401/403 means a key or billing problem. |
| Excel/PDF links open to a login page | Expected: reports need a signed-in reviewer. Set `PUBLIC_BASE_URL` to the real address. |
| C01 always N/A | The module code in the file name has no reference content under Modules. |

### Python notes

Use Python 3.11–3.13. On this Mac the Homebrew Pythons have a broken XML library (`pyexpat`); `lxml` is a
declared dependency so Excel generation still works, but reading `.xlsx` files back in tests falls back to a
structural check. If `make setup-live` fails on `ensurepip`, create the venv with a different Python build.

## Known limits

- The live Gemini calls were **verified against the installed SDK's types (google-genai 2.34) and tested with a
  fake client**, but not against the real API, because no key was available. `make doctor` makes the first real
  call; run one approved sample before any batch.
- Not built yet: Batch API path (`CORPUS_API_PATH=batch` is refused until a spike passes), the 30-video pilot
  harness (MAE, weighted kappa, GO/NO-GO), conditional re-inspection of low-confidence competencies.
- Mentee names are personal data (India DPDP Act 2023): get consent for AI-assisted assessment, restrict the
  Participants sheet, and note that deleting a session also deletes its names.
- Single-site, password-based access. For wider rollout put it behind your SSO or reverse proxy.
