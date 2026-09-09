# MokshaImage — Photo Handout Platform

A system for running a photo-retouching outsourcing operation: pull the client's
raw images out of their S3 bucket, hand a fixed share of them to each freelance
retoucher, collect the finished files back through Google Drive, put them
through quality control, and report on who did how much and how well.

This document is the complete handover. It explains what the system does, how it
is built, every file in the repo, how to run it locally, how it is deployed, what
secrets you need (and how to get them), and what is unfinished.

---

## 1. Context: the problem this solves

The client (a car-photography pipeline) drops retouching jobs into an S3 bucket,
organised by brand and date. Each "photo" is really **two files** — a `.psd`
working file and its `_orig.jpg/.jpeg` original. Both share a base name, and
throughout this system they are counted as **one photo**.

The operation needs to:

1. Take *N* photos for a brand and split them across freelancers, so no photo is
   ever given to two people.
2. Get the files to freelancers who don't (and shouldn't) have S3 credentials.
3. Get finished files back, matched automatically to what was assigned.
4. Have a QC person check the work, correct what's wrong, and record an error rate.
5. Produce a per-worker report for a date range (for payment and performance).

---

## 2. The two halves of this repo

The project grew in two stages. **Both are in this repo and both work**, but they
are separate systems solving the same problem at different scales.

### Half A — the desktop tool (the original, superseded)

A single-admin tool that runs on one Windows PC. The admin downloads photos to a
local `pool/` folder, splits them into per-freelancer ZIPs, uploads each ZIP to
Google Drive, and sends each freelancer a share link. State lives in a JSON
ledger inside `pool/`.

Files: `download_s3.py`, `distribute.py`, `webform.py`, `drive_auth.py`,
`drive_upload.py`, and the `.bat` launchers.

**Status:** working, but superseded by Half B. Freelancers get a Drive link and
send finished work back by whatever means — there is no tracking of individual
photos, no QC, and no reporting. Keep it as a fallback / reference; the newer
platform does not depend on it.

### Half B — the hub platform (current, deployed)

A multi-user web app (`hub/`) deployed on Railway with Postgres. Admin, workers,
and QC each log in and see their own view. Every individual photo is tracked
through its whole life in the database.

**This is the system that matters.** The rest of this document is mostly about it.

**Key design decision — photo bytes never pass through the server.** The server
only issues short-lived links:

- Workers **download** via S3 presigned URLs (they never hold an S3 key).
- Workers **upload** via Google Drive resumable-upload sessions (bytes go
  straight from their machine to Google).

This keeps the Railway service tiny and cheap regardless of how many gigabytes of
imagery move through the operation, and means no credentials ever reach a
freelancer's PC.

---

## 3. How the platform works, end to end

```
   S3 (client's bucket)                      Google Drive (our account)
   download-retouching-files/                PhotoHandout Uploads/
        |                                            ^        |
        | presigned GET                              | upload | download
        |  (short-lived)                             | session| (QC review)
        v                                            |        v
   +---------+        assign          +---------+          +------+
   | WORKER  | <--------------------- |  ADMIN  |          |  QC  |
   +---------+                        +---------+          +------+
        |        upload finished           ^                   |
        +--------------------------------->|<------------------+
                                    metrics / report
             Railway (Flask + Postgres) — coordination only
```

### The flow, step by step

1. **Admin assigns.** Admin picks a brand, a worker, a count, and optionally a QC
   reviewer. The platform lists S3 for that brand (newest dates first), skips
   every photo already claimed by *any* assignment, and reserves exactly that many
   unclaimed photo IDs. If S3 doesn't have enough, it reserves what it can and
   reports the shortfall (`short`). This reservation is why the same photo can
   never be handed to two workers.

2. **Worker downloads.** The worker logs in, sees "my share", and clicks Download.
   The server streams the photos' S3 objects into a single organised ZIP
   (image + PSD in folders) and sends it. Those photos flip to `downloaded`.

3. **Worker uploads.** The worker retouches, then uploads finished files in the
   browser. The platform matches each uploaded filename to an assigned photo by
   **base name** — extension and `_orig` suffix stripped, so `abc-v4_orig.jpeg`
   and `abc-v4.psd` both resolve to `abc-v4`. Matched photos flip to `uploaded`
   and store their Drive link; unmatched filenames are reported back so a typo
   doesn't silently vanish. Assigned photos with no matching upload simply stay
   "not done".

4. **QC reviews.** Once the whole batch is uploaded, the assigned QC gets a batch
   view: a grid of the worker's finished photos. They click **Download all** to
   get the batch as one ZIP (streamed from Drive), review and fix locally, then
   upload the whole corrected set back with two numbers — **Total** checked and
   **Corrected**. Photos in the re-uploaded set are marked Approved, or *Corrected
   by QC* for the filenames the QC flagged. The QC's file replaces the worker's,
   and the worker's now-stale Drive file is deleted. `corrected / total` is the
   **batch error rate**. The worker and the admins get an in-app notification.

   There is also a per-photo path (Okay / Reject with remark + screenshot /
   Rectify). A rejected photo returns to `downloaded` for the worker to redo.

5. **Admin reports.** Admin picks a date range and downloads an Excel workbook:
   a **Summary** sheet (per-worker overview with error rate, then a master list of
   every image), plus **one sheet per worker** with every image, allotment /
   upload / re-upload timestamps, the QC verdict, error details, and which QC
   assessed it. All times are UTC, matching the dashboards.

### Extra admin controls

- **Reassign a worker's unfinished photos.** If someone drops out mid-batch,
  admin moves everything *not yet uploaded* to another worker as a brand-new
  assignment (same brand and QC). Photos already uploaded stay on the original
  assignment, so the first worker keeps credit for what they actually did. The
  moved photos reset to `assigned` so the new worker downloads them fresh.
- **Assign / change / clear the QC** on an existing assignment.
- **Create workers and QCs**, and see team counts.

---

## 4. Data model (`hub/models.py`)

Five tables hold the entire state of the platform.

| Table | What it holds |
|---|---|
| `workers` | Everyone who logs in — admin, workers, QCs. `role` is `admin`/`worker`/`qc`. Passwords are werkzeug hashes, never plaintext. |
| `assignments` | "This worker owes N photos of brand X", plus the QC reviewing the batch. |
| `assignment_photos` | **One row per assigned photo** — the heart of the system. Tracks status, Drive link, timestamps, and QC verdict. |
| `qc_reviews` | One whole-batch QC report: total checked, how many corrected, note to the worker. |
| `notifications` | One-line in-app messages shown as a dismissable banner. |

### Photo lifecycle

`assigned` → `downloaded` → `uploaded`

A **rejected** photo returns to `downloaded` for redo. Verification is just
counting rows by status — there is no separate progress counter that can drift
out of sync.

### QC verdict, layered on top of an uploaded photo

- `ok` — approved as-is
- `reject` — sent back to the worker to redo
- `rectified` — the QC fixed it themselves and re-uploaded

### Fields worth understanding

- **`photo_id`** — the pool-relative base ID shared by a photo's two files, e.g.
  `catchall_ireland/20260716/exterior-1/.../<name>-v4`. This is the join key
  between S3 and the database. `hub/s3source.py` derives it identically to the
  desktop tool.
- **`first_uploaded_at` / `reuploaded_at` / `uploaded_at`** — kept separately so
  a redo doesn't erase the original submission time. `uploaded_at` is the most
  recent; `first_uploaded_at` is the first submission.
- **`reject_count`** — survives the redo loop, unlike `qc`, which is cleared when
  the photo is re-uploaded. This is what makes the error rate honest.
- **`qc_by_id`** — stamped per photo, because a worker's reviewer can change day
  to day.

### Schema migrations

There is no Alembic. `hub/db.py::_add_missing_columns()` is a lightweight
migration that runs on every startup: `create_all` only creates missing *tables*,
not missing *columns*, so this `ALTER TABLE ... ADD COLUMN`s anything new. It is
idempotent and safe to run repeatedly.

**When you add a column to a model, you must also add it to the `wanted` dict in
`_add_missing_columns()`,** or production will break while local SQLite (created
fresh) works fine. This is the single most likely thing to trip you up.

---

## 5. File-by-file guide

### `hub/` — the platform (current system)

| File | Lines | What it does |
|---|---|---|
| `app.py` | 992 | The whole Flask web app: routes, auth guards, and all HTML (inline via `render_template_string` — there is no `templates/` dir). Login, admin dashboard, worker "my share", QC batch pages, report download, and the JSON API for the companion. |
| `engine.py` | 684 | All the business logic, DB-backed. Assign, reserve, status transitions, verification, QC batch application, reporting, notifications. **Start here to understand the system.** |
| `models.py` | 137 | The five SQLAlchemy tables and the status/role constants. |
| `db.py` | 53 | Engine/session setup, Postgres-or-SQLite selection, and the startup column migration. |
| `auth.py` | 57 | Create users, authenticate, list, and `seed_admin()` — which guarantees one admin exists on first boot so the platform is usable immediately. |
| `s3source.py` | 107 | Read-only view of the client's bucket: list brands and dates, derive photo IDs, find unclaimed photos, mint presigned URLs, stream objects into a ZIP. Self-contained (mirrors the desktop tool's ID logic) so the platform deploys alone. |
| `drive.py` | 161 | Google Drive: holds the credentials, mints short-lived resumable-upload sessions, streams files into ZIPs, deletes superseded files. Reads creds from `GOOGLE_TOKEN_JSON` (Railway) or `token.json` (local). |
| `report.py` | 165 | Turns `engine.report_data()` into a formatted Excel workbook (openpyxl) — Summary sheet plus one sheet per worker. |
| `companion.py` | 170 | Optional CLI that runs on a worker's own PC: signs in over the API, downloads their assigned photos into `./worker_pool/`, uploads finished work. Largely superseded by the in-browser download/upload buttons. |
| `test_phase1.py` | 57 | A smoke test of the early download phase. Not a full test suite. |

### Root — the desktop tool (Half A, superseded)

| File | What it does |
|---|---|
| `download_s3.py` | Downloads one brand's photos from S3 into `./pool/<brand>/<date>/...`, newest date first. Skips already-downloaded files (same size), so re-running is safe. Flags: `--dry-run`, `--recent N`, `--date`, `--list-dates`, `--photos N`, `--types`. |
| `distribute.py` | Splits the pool into per-freelancer ZIPs. Photos are dealt off the top in order; leftovers stay for next time. Every handout is recorded in `pool/_ledger.json` so nothing is issued twice. Also exposes `scan_available()` / `assign()` for the web form. |
| `webform.py` | A small local Flask page (port 5000) for the admin to preview and create splits, with background zipping and Drive upload. Links appear as each upload finishes. A freelancer's photos are marked handed-out **only after** their upload succeeds, so a failed upload never consumes photos. |
| `drive_auth.py` | One-time Google OAuth flow. Opens a browser, saves `token.json`. Uses the `drive.file` scope — it can only touch files this app created, never the rest of the Drive. |
| `drive_upload.py` | Uploads a ZIP to Drive and returns a shareable link. |
| `brands.txt` | The brand list that populates the admin's Brand dropdown. |
| `*.bat` | Windows launchers so a non-technical admin never touches a terminal: `Setup (run once)`, `Authorize Google Drive (run once)`, `Download Photos`, `Start Photo Handout`. |

### Config and docs

| File | What it does |
|---|---|
| `requirements.txt` | Python dependencies. |
| `Procfile` | Railway start command: `gunicorn hub.app:app`. |
| `mise.toml` | Pins Python 3.12; disables the GitHub attestation check that broke the Railway build. |
| `.gitignore` | Excludes all secrets and local data. **Do not weaken this.** |
| `HOW TO USE.txt` | End-user instructions for the desktop tool. |
| `SETUP ON NEW PC.txt` | One-time desktop-tool setup for a non-technical user. |
| `DEPLOY-TO-RAILWAY.txt` | The original deploy notes (superseded by §8 below, kept for reference). |

### Generated / ignored (not in git)

`pool/` (downloaded photos + ledger), `zips/` (built ZIPs), `worker_pool/`
(companion downloads), `*.db` (local SQLite), `*.log`, `__pycache__/`.

---

## 6. Secrets — what you need, and how to get it

**No secret is in this repo, and none ever has been** (verified against the full
git history). The four items below must be handed over **out of band** — never
commit them.

| Secret | What it is | How to obtain / rotate |
|---|---|---|
| **AWS access key + secret** | Read access to the client's S3 bucket `carcutter-mumbai`, prefix `download-retouching-files/`, region `ap-south-1`. | Issued by the client. Ask them to rotate to a new key for the new owner and revoke the old one. |
| **`client_secret.json`** | Google OAuth *client* credentials for the Drive app. | Google Cloud Console → the project's OAuth 2.0 Client ID (Desktop app type). The new owner should either be added to the existing GCP project or create their own client. |
| **`token.json`** | The Drive *user* token — which Google account the finished files land in. | Generated by running `python drive_auth.py` once and approving in the browser. If Drive stops working, the token was revoked: regenerate it and update `GOOGLE_TOKEN_JSON` on Railway. |
| **Railway account + `ADMIN_PASSWORD`** | The deployment and the seeded admin login. | Transfer the Railway project to the new owner, or have them redeploy from this repo into their own account. |

**The Google consent screen is in testing mode**, which is why users see "app is
not verified" during authorization (click *Advanced → Go to … (unsafe) → Allow*).
That is expected for an in-house tool. Testing-mode refresh tokens can expire; if
Drive uploads start failing, regenerate `token.json` first.

---

## 7. Running locally

```bash
git clone https://github.com/akkshattshah/mokshaimageediting.git
cd mokshaimageediting
python -m venv .venv
.venv/Scripts/activate            # Windows;  source .venv/bin/activate on mac/linux
pip install -r requirements.txt
```

**S3 credentials** — either set `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in
the environment, or create `~/.aws/credentials`:

```ini
[default]
aws_access_key_id = ...
aws_secret_access_key = ...
region = ap-south-1
```

boto3 picks up either automatically.

**Google Drive** — put `client_secret.json` in the project root and run
`python drive_auth.py` once. It saves `token.json`.

**Start the platform:**

```bash
python -m hub.app          # http://127.0.0.1:5001
```

With no `DATABASE_URL`, it creates and uses a local SQLite file `hub_dev.db` —
the same code path as production. It seeds an admin on first boot from
`ADMIN_LOGIN` / `ADMIN_PASSWORD` (defaults `admin` / `admin123` — change these).

**Start the old desktop tool** (only if you need it):

```bash
python webform.py          # http://127.0.0.1:5000
```

---

## 8. Deployment (Railway)

The platform runs as one Railway service plus a Postgres plugin. Only `hub/` is
deployed; the desktop tool is not.

1. **New Project → Deploy from GitHub repo** → pick this repo.
2. **New → Database → Add PostgreSQL.** Railway injects `DATABASE_URL`
   automatically — you don't copy it anywhere. (`hub/db.py` rewrites Railway's
   `postgres://` to the `postgresql://` that SQLAlchemy expects.)
3. Railway detects Python, installs `requirements.txt`, and runs the `Procfile`.
   Tables are created on first boot.
4. **Service → Variables:**

   | Variable | Value |
   |---|---|
   | `SECRET_KEY` | any long random string (Flask session signing) |
   | `ADMIN_LOGIN` | `admin` |
   | `ADMIN_PASSWORD` | a strong password |
   | `AWS_ACCESS_KEY_ID` | the S3 access key |
   | `AWS_SECRET_ACCESS_KEY` | the S3 secret |
   | `AWS_REGION` | `ap-south-1` |
   | `S3_BUCKET` | `carcutter-mumbai` |
   | `S3_PREFIX` | `download-retouching-files/` (optional — this is the default) |
   | `GOOGLE_TOKEN_JSON` | the **entire contents** of `token.json`, pasted as one line |
   | `DRIVE_FOLDER` | `PhotoHandout Uploads` (optional) |

5. **Set a spend limit** — Project/Account → Usage → Set limits.
6. **Settings → Networking → Generate Domain**, then sign in as admin and add
   workers.

**Redeploys wipe Railway's local disk, and that is fine** — all state is in
Postgres and the Google creds come from `GOOGLE_TOKEN_JSON`. Nothing important
lives on the container's filesystem.

`ADMIN_PASSWORD` only affects the **first** seed. After that, manage accounts
from the admin screen; changing the variable won't change an existing password.

---

## 9. Operating notes and gotchas

- **A "photo" is always the pair** (`.psd` + `_orig` image) counted as one. Every
  count in the UI, the reservations, and the reports use this rule.
- **Reservations are permanent.** `claimed_photo_ids()` excludes photos claimed by
  *any* assignment for a brand, forever. There is currently **no way to release a
  reservation** from the UI — deleting the assignment row is the only route. Worth
  knowing before you assign 5,000 photos by accident.
- **Filename matching is by base name.** If a worker renames files, the upload
  won't match and will be reported as unmatched. Tell workers to keep filenames.
- **All times are UTC**, in both the dashboards and the Excel export. There is no
  IST conversion — a likely first request from whoever takes over.
- **The Excel report date range is `[start, end)`** — end-exclusive internally,
  though the label shows the inclusive last day.
- **`brands.txt` is read from either `hub/` or the project root.** It only feeds
  the dropdown; the authoritative brand list comes from S3.
- **Drive scope is `drive.file`** — the app can only see files it created. It
  cannot read the rest of the account's Drive, by design.
- **Uploads feel slow?** That is the freelancer's upload bandwidth, not the app —
  bytes go straight to Google, not through the server.

---

## 10. Known gaps / where to pick up

These are genuinely unfinished, listed so the next owner isn't surprised:

1. **No automated test suite.** `hub/test_phase1.py` is a smoke test of the early
   download phase only. The QC and reporting logic has no coverage.
2. **All HTML is inline** in `app.py` via `render_template_string` (992 lines).
   Splitting it into real templates is the obvious first refactor.
3. **No way to un-reserve photos** from the UI (see §9).
4. **No Alembic** — migrations are the hand-rolled column-adder in `db.py`. Fine
   at this size, but remember the rule in §4.
5. **The companion CLI is half-superseded** by the in-browser buttons and isn't
   packaged into a one-click app. Workers need Python to use it, which is why
   most use the browser instead.
6. **Google OAuth is in testing mode** — tokens can expire; verification would fix
   that permanently.
7. **The desktop tool (Half A) and the platform (Half B) share no code.**
   `s3source.py` deliberately duplicates the photo-ID logic so the platform can
   deploy standalone. If you change the ID rule, change it in both places.

---

## 11. Handover checklist

- [ ] New owner has repo access (or ownership transferred).
- [ ] AWS key rotated to a new key for the new owner; old key revoked.
- [ ] `client_secret.json` handed over, or new owner added to the GCP project.
- [ ] `token.json` regenerated by the new owner (`python drive_auth.py`) against
      the Google account that should receive finished files.
- [ ] Railway project transferred, or redeployed under the new owner's account
      with all variables from §8 set.
- [ ] `SECRET_KEY` and `ADMIN_PASSWORD` changed to new values.
- [ ] Client informed of the operational contact change.
- [ ] Old owner's access removed everywhere: AWS, GCP, Railway, GitHub, Drive.
