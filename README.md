# SCD Effort Reporting
This is a Django based web application for the Fermilab Scientific Computing Division.  It's intent is to allow employees to log and review daily/weekly/monthly effort reports. Staff memebers can entries describing work that was performed against different projects and categories and then then managers can review, filter, aggregate, summarize, preview, and export the parts of the data or full reports.

The application is designed to work in the Fermilab security environment and with the on premisses OKD cluster.  This makes it a lightweight, easily maintainable and deployable part of our portfolio.

**Current release: v01.00.00**

---

## Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Tech Stack](#tech-stack)
- [Quickstart](#quickstart)
- [Local Development](#local-development)
- [Running Tests](#running-tests)
- [Docker Deployment](#docker-deployment)
- [User Roles](#user-roles)
- [Email Reminders](#email-reminders)
- [URL Reference](#url-reference)
- [Environment Variables](#environment-variables)
- [API](#api)
- [Management Commands](#management-commands)

---

## Features

- **Effort entries** — submit and manage weekly/biweekly/monthly work entries with Markdown descriptions, tags, period dates, group assignment, and flags for privacy, criticality, and highlight rating (0–5 stars)
- **HTMX interactions** — period chip selector pre-fills date fields; tag autocomplete with chip UI; live Markdown preview; all without full page reloads
- **Role-based access** — six roles (User, Group Leader, Division Head, Functional Lead, Auditor, Administrator) with enforced permission checks throughout; group leaders and division heads see reports scoped to their managed groups
- **Reports** — admins, auditors, group leaders, division heads, and functional leads filter entries by author, project, category, and date range; preview results with per-row checkboxes to select a subset; download as plain text, CSV, JSON, XLSX, or PDF; report scope is automatically restricted to managed groups/projects for non-admin roles
- **AI Summary** — generate a structured narrative summary of any filtered or selected entries via the Anthropic API; download as plain text or PDF; system prompt and user template are editable from the web UI by admins
- **Email reminders** — functional leads and above see the people in their scope with the date of each person's last entry, and can mail an activity-report reminder to individuals, to everyone who is overdue, or to the whole scope; the email body is edited in the web UI with `{{placeholder}}` tokens and a live preview, and recurring sends (for example every Friday at 15:00) are configured per lead. Delivery is driven by an OKD CronJob calling `manage.py send_reminders`, is safe to run concurrently, and every attempt is recorded
- **Audit log** — WorkItem create/update/delete/reassign/archive events, login/logout events, and report exports (including AI summaries) are recorded with actor, IP address, user-agent, and field-level diffs
- **Taxonomy management** — projects, categories, and organisational groups are managed through a tabbed web UI (Admin role required)
- **User management** — administrators can create accounts, assign roles, reset passwords, and delete users; group leaders and division heads see only their own group's members
- **API token management** — users can generate, rotate, and revoke personal API tokens from their profile; admins can view and revoke all tokens from a dedicated admin page
- **REST API** — authenticated entry creation via `POST /api/entries/` using a Bearer token; suitable for scripted or automated submission
- **Bug reporting** — authenticated users can submit bug reports directly to the GitHub repository from within the application; submissions use a GitHub App installation token (no personal credentials required) and are rate-limited per user
- **SSO** — local email/password auth via django-allauth; optional OIDC single sign-on (Fermilab Keycloak / CILogon); the SSO button appears only when OIDC credentials are configured

---

## Architecture

```
apps/
├── core/        Dashboard, base templates, markdown renderer, bug report, about/API pages
├── accounts/    Custom User model (role + employee_id), profile, admin user management, API tokens
├── taxonomy/    Project, Category, WorkGroup, LabPriority, Tag models; autocomplete
├── entries/     WorkItem CRUD, archive/unarchive, manager reassignment, REST API
├── reports/     Filter form, five exporters, row selection, AI summary, HTMX preview
├── audit/       AuditLogEntry model, log_event service, signals, middleware, viewer
└── reminders/   Reminder templates, schedules, scoped recipient page, delivery, send log
```

Each app is independently namespaced and has its own URLs, templates, and tests.

---

## Tech Stack

| Layer | Choice |
|---|---|
| Framework | Django 5 |
| Auth | django-allauth ≥ 65 (local + OIDC/CILogon) |
| Frontend | Tailwind CSS 3 via django-tailwind, HTMX 2 |
| Database | PostgreSQL 16 (production), SQLite (local dev fallback) |
| PDF export | reportlab (pure Python, no system dependencies) |
| Markdown sanitising | nh3 (Rust `ammonia` bindings; HTML5-conformant allowlist sanitizer) |
| XLSX export | openpyxl |
| AI summary | Anthropic Python SDK (claude-sonnet-5 by default) |
| Filtering | django-filter |
| GitHub integration | PyJWT + GitHub App installation tokens |
| Container | Docker multi-arch (linux/amd64 + linux/arm64) |
| Orchestration | OKD via Helm (`helm/simple/`) |
| Tests | pytest-django (145 tests) |

---

## Quickstart

The fastest way to get running — one command handles everything:

```bash
# macOS / Linux
git clone https://github.com/fermitools/SCD-Reporting.git
cd SCD-Reporting
./bootstrap.sh --admin-password yourpassword
```

```powershell
# Windows (PowerShell)
git clone https://github.com/fermitools/SCD-Reporting.git
cd SCD-Reporting
.\bootstrap.ps1 -AdminPassword yourpassword
```

The script creates a virtual environment, installs dependencies, migrates the database, seeds initial data, and starts the server at <http://localhost:8000>. Log in with `scd-admin@fnal.gov` and the password you provided.

To enable the AI Summary feature, pass your Anthropic API key:

```bash
./bootstrap.sh --admin-password yourpassword --anthropic-key sk-ant-...
```

For platform-specific prerequisites and manual setup steps see **[docs/quickstart.md](docs/quickstart.md)**.

---

## Local Development

### Prerequisites

- Python 3.12+
- Node.js 20+ (optional — Tailwind falls back to the Play CDN in dev mode)
- Git

### Setup

The `bootstrap.sh` / `bootstrap.ps1` script handles all of the steps below automatically. Use the manual steps if you need finer control or are integrating with an existing environment.

```bash
# Create and activate a virtual environment
python3.12 -m venv venv
source venv/bin/activate        # Windows: .\venv\Scripts\Activate.ps1

# Install Python dependencies
pip install -r requirements.txt

# Install Node dependencies for Tailwind (optional)
cd theme/static_src && npm install && cd ../..
```

### Database

The default development configuration uses SQLite — no database server required.

```bash
python manage.py migrate
python manage.py seed_taxonomy
SCD_INITIAL_ADMIN_PASSWORD=yourpassword python manage.py seed_admin
```

### Run the development server

```bash
python manage.py runserver
```

The app is available at <http://localhost:8000>. Log in with `scd-admin@fnal.gov` and your chosen password.

> The development settings (`scd_reporting.settings.dev`) enable `DEBUG=True`, load Tailwind from the Play CDN, and log emails to the console.
>
> For CSS hot-reload during frontend work, run `cd theme/static_src && npm start` in a second terminal.

### AI Summary

To enable the AI Summary feature, export your Anthropic API key before starting the server:

```bash
export ANTHROPIC_API_KEY=sk-ant-...          # Windows: $env:ANTHROPIC_API_KEY="sk-ant-..."
python manage.py runserver
```

Without the key the summary button returns an error in the UI; all other features work normally.

To route requests through a custom endpoint (e.g. a LiteLLM proxy):

```bash
export ANTHROPIC_BASE_URL=https://litellm.example.org
export ANTHROPIC_SUMMARY_MODEL=azure/claude-sonnet-5
```

---

## Running Tests

```bash
# Run the full test suite
pytest tests/

# Run with coverage
pytest tests/ --cov=apps --cov-report=term-missing

# Run a single test module
pytest tests/test_entries.py -v
```

Tests use `scd_reporting.settings.dev` and an in-memory SQLite database. No external services are required.

Current baseline: **145 tests, all passing.**

---

## Docker Deployment

See [DEPLOY.md](DEPLOY.md) for the full OKD/Helm deployment guide.

Quick start for local Docker Compose:

```bash
cp .env.example .env
# Edit .env — set POSTGRES_PASSWORD, DJANGO_SECRET_KEY, SCD_INITIAL_ADMIN_PASSWORD, SCD_HOSTNAME
docker compose up -d
```

For OKD, use the deploy script:

```bash
./scripts/deploy.sh -t v01.00.00 -f my-values.yaml
```

See `./scripts/deploy.sh --help` for all flags.

---

## User Roles

| Role | Description | Key Permissions |
|---|---|---|
| **User** | Regular SCD staff | Submit and manage their own entries; view their own dashboard |
| **Group Leader** | Team lead | All User permissions + view/manage entries for their group; run reports scoped to their group; send report reminders to their group; view audit log; view and assign users within their group |
| **Division Head** | Division manager | All Group Leader permissions + scope spans all managed groups |
| **Functional Lead** | Project/function lead | All User permissions + run reports scoped to their managed projects; view entry details; send report reminders to people who booked effort against their projects |
| **Auditor** | Read-only oversight | View all non-restricted entries; run reports; generate AI summaries; view audit log — no write access, and no reminder sending |
| **Administrator** | Full access | All permissions + manage taxonomy, manage all user accounts and roles, view all API tokens, configure AI prompts |

Role assignment is done via the Admin Users page (`/admin-users/`) by an Administrator.

---

## Email Reminders

Reminders nudge people to file an activity report. They carry a boiler-plate
body, a link back to the reporting interface, and the date and title of the
recipient's most recent entry — or a note that there is none on record.

### Who can send, and to whom

Recipients are resolved from the sender's role every time, using the same
scoping rules as reports:

| Role | Reminder scope |
|---|---|
| Administrator | Every active user with an email address |
| Division Head | Users whose primary group is one they manage |
| Group Leader | Users in their own primary group |
| Functional Lead | Authors of entries booked against a project they lead |
| Auditor | *none* — read-only role, so it cannot send |
| User | *none* |

A recipient id posted for somebody outside the sender's scope is filtered out
rather than rejected, so the scope is enforced by the query, not by the form.

### Pages

| Path | Purpose |
|---|---|
| `/reminders/` | People in scope, with last-entry date, days since, entry count, and last reminded; send to selected, to everyone overdue, or to the whole scope |
| `/reminders/templates/` | Edit the email subject and body; Markdown with `{{placeholder}}` tokens and a live preview |
| `/reminders/schedules/` | Recurring sends — weekly, biweekly, or monthly, at a wall-clock time in a named timezone; enable/disable, dry run, run now |
| `/reminders/log/` | Every attempt, with the skip or failure reason |

### Placeholders

`{{recipient_name}}`, `{{recipient_email}}`, `{{recipient_group}}`,
`{{last_entry_date}}`, `{{last_entry_title}}`, `{{last_entry_summary}}`,
`{{days_since_last_entry}}`, `{{entry_count}}`, `{{report_url}}`,
`{{new_entry_url}}`, `{{my_entries_url}}`, `{{sender_name}}`, `{{today}}`.

Substitution is a plain token replacement, not a Django template render — the
body is operator-supplied text and must not be able to execute anything. An
unrecognised token is left in place, so a typo shows up in the preview rather
than silently blanking a line. The HTML alternative goes through the same
nh3 sanitiser as entry descriptions.

### How scheduled sends actually fire

The schedule row stores the wall-clock rule; a driver process periodically asks
"what is due?". Three drivers, one code path:

```bash
# 1. OKD CronJob — the supported production driver (helm/simple, on by default)
#    reminders.cronjob.schedule: "*/15 * * * *"

# 2. System cron
*/15 * * * * cd /app && python manage.py send_reminders >> logs/reminders.log 2>&1

# 3. In-process daemon thread, for deployments with nowhere to run cron
REMINDER_SCHEDULER_ENABLED=1
```

Running these concurrently is safe. Each due schedule is claimed with a single
atomic conditional `UPDATE ... WHERE next_run_at <= now`, which is atomic on
both SQLite and PostgreSQL, so exactly one of three gunicorn workers plus a
cron tick ends up sending it. The claim is taken before any mail leaves, so a
crash mid-batch skips that firing rather than replaying it — a missed nudge
costs much less than a duplicate.

Two further guards against over-mailing: a recipient reminded within
`REMINDER_MIN_INTERVAL_HOURS` (default 20) is skipped, and a schedule with
"only remind people who are overdue" set skips anyone with an entry inside its
staleness window.

`REMINDER_BASE_URL` must be set for scheduled sends. A scheduled run has no
HTTP request to derive the site address from, so without it the links in the
mail come out relative and useless; the Helm chart derives it from
`route.hostname` automatically. The reminder pages show a warning banner when
it is unset.

### Operating it

```bash
# What exists, and what is due right now
python manage.py send_reminders --list

# Rehearse everything without sending, logging, or advancing any schedule
python manage.py send_reminders --dry-run

# Force one schedule ahead of its slot
python manage.py send_reminders --schedule 3 --force

# Check SMTP and the rendered body against a real mailbox
python manage.py send_reminders --recipient you@fnal.gov --ignore-interval

# Inside the running pod
oc -n scd-reporting exec deploy/web -- python manage.py send_reminders --list
```

Full option reference: `man docs/man/scd-send-reminders.1`.

Settings live in `config/reminders.yaml` (copy `config/reminders.yaml.example`)
and are overridden by environment variables, which are in turn overridden by
the command-line flags above.

---

## URL Reference

### Public / authenticated

| Path | Description | Access |
|---|---|---|
| `/` | Dashboard | Authenticated |
| `/accounts/login/` | Login | Public |
| `/accounts/signup/` | Sign up | Public (can be disabled) |
| `/profile/` | Edit profile, manage API token | Authenticated |
| `/profile/api-token/rotate/` | Generate or rotate API token (POST) | Authenticated |
| `/profile/api-token/revoke/` | Revoke API token (POST) | Authenticated |
| `/select-group/` | First-login group selection | Authenticated |
| `/about/` | About page and dependency list | Authenticated |
| `/api/` | API documentation | Authenticated |
| `/bug-report/` | Submit a bug report | Authenticated |

### Entries

| Path | Description | Access |
|---|---|---|
| `/entries/` | My entries list | Authenticated |
| `/entries/new/` | Create entry | Authenticated |
| `/entries/<pk>/` | Entry detail | Owner / Group Leader / Auditor / Admin |
| `/entries/<pk>/edit/` | Edit entry | Owner |
| `/entries/<pk>/delete/` | Delete entry | Owner |
| `/entries/<pk>/archive/` | Archive / unarchive (POST) | Group Leader / Division Head / Admin |
| `/entries/manage/` | Manage all entries (reassign, archive) | Group Leader / Division Head / Admin |

### Reports

| Path | Description | Access |
|---|---|---|
| `/reports/` | Report filter, preview, export, AI summary | Auditor+ |
| `/reports/preview/` | HTMX preview partial (POST) | Auditor+ |
| `/reports/download/<fmt>/` | Download (txt csv json xlsx pdf) | Auditor+ |
| `/reports/summary/` | Generate AI summary (POST, HTMX) | Auditor+ |
| `/reports/summary/download/txt/` | Download AI summary as plain text | Auditor+ |
| `/reports/summary/download/pdf/` | Download AI summary as PDF | Auditor+ |
| `/reports/prompt-config/` | Save AI prompt configuration (POST) | Admin |

### Reminders

| Path | Description | Access |
|---|---|---|
| `/reminders/` | People in scope with reporting freshness | Functional Lead+ |
| `/reminders/send/` | Send reminders (POST) | Functional Lead+ |
| `/reminders/templates/` | List email bodies | Functional Lead+ |
| `/reminders/templates/new/` | Create an email body | Functional Lead+ |
| `/reminders/templates/<pk>/edit/` | Edit an email body | Functional Lead+ |
| `/reminders/templates/<pk>/default/` | Make it the default (POST) | Functional Lead+ |
| `/reminders/templates/<pk>/delete/` | Delete an email body (POST) | Functional Lead+ |
| `/reminders/templates/preview/` | HTMX body preview (POST) | Functional Lead+ |
| `/reminders/schedules/` | Recurring sends | Functional Lead+ (own), Admin (all) |
| `/reminders/schedules/new/` | Create a schedule | Functional Lead+ |
| `/reminders/schedules/<pk>/edit/` | Edit a schedule | Owner / Admin |
| `/reminders/schedules/<pk>/toggle/` | Enable or disable (POST) | Owner / Admin |
| `/reminders/schedules/<pk>/run/` | Run now, or dry run (POST) | Owner / Admin |
| `/reminders/schedules/<pk>/delete/` | Delete a schedule (POST) | Owner / Admin |
| `/reminders/log/` | Delivery history, scoped | Functional Lead+ |

### Admin

| Path | Description | Access |
|---|---|---|
| `/admin-users/` | User directory | Admin / Division Head / Group Leader |
| `/admin-users/create/` | Create user | Admin |
| `/admin-users/<pk>/role/` | Update user role (HTMX) | Admin |
| `/admin-users/<pk>/primary-group/` | Set primary group (HTMX) | Admin / Division Head / Group Leader |
| `/admin-users/<pk>/managed-groups/` | Set managed groups (HTMX) | Admin |
| `/admin-users/<pk>/managed-projects/` | Set managed projects (HTMX) | Admin |
| `/admin-users/<pk>/set-password/` | Reset password | Admin |
| `/admin-users/<pk>/delete/` | Delete account | Admin |
| `/admin-api-tokens/` | View all API tokens | Admin |
| `/admin-api-tokens/<pk>/revoke/` | Revoke any token (POST) | Admin |
| `/audit/` | Audit log viewer | Auditor+ |
| `/taxonomy/` | Taxonomy management | Admin |
| `/admin/` | Django admin | Superuser |

### REST API

| Path | Method | Description | Auth |
|---|---|---|---|
| `/api/entries/` | POST | Create an entry | Bearer token |

---

## Environment Variables

### Required in production

| Variable | Description |
|---|---|
| `DJANGO_SECRET_KEY` | Django cryptographic key |
| `SCD_INITIAL_ADMIN_PASSWORD` | Password for the initial admin account |

### Optional / have defaults

| Variable | Default | Description |
|---|---|---|
| `DJANGO_SETTINGS_MODULE` | *(set explicitly)* | Use `scd_reporting.settings.prod` in production |
| `DJANGO_ALLOWED_HOSTS` | `localhost` | Comma-separated valid `Host` header values |
| `DATABASE_URL` | `sqlite:///db.sqlite3` | Full database connection URL (dj-database-url format) |
| `GUNICORN_WORKERS` | `3` | Number of Gunicorn worker processes |
| `SCD_INITIAL_ADMIN_USERNAME` | `scd-admin` | Username of the bootstrapped admin |
| `SCD_INITIAL_ADMIN_EMAIL` | `scd-admin@fnal.gov` | Email of the bootstrapped admin |
| `SCD_DISABLE_LOCAL_SIGNUP` | `0` | Set to `1` to block new local accounts |
| `ACCOUNT_EMAIL_VERIFICATION` | `optional` | allauth email verification: `none`, `optional`, `mandatory` |
| `ANTHROPIC_API_KEY` | *(empty)* | Anthropic API key; required for AI summary feature |
| `ANTHROPIC_SUMMARY_MODEL` | `claude-sonnet-5` | Model used for report summaries |
| `ANTHROPIC_MAX_TOKENS` | `16000` | Output ceiling for a generated summary; a truncated summary is flagged in the UI |
| `ANTHROPIC_MAX_INPUT_TOKENS` | `150000` | Largest prompt the summariser will send; `0` disables the check |
| `ANTHROPIC_BASE_URL` | *(empty)* | Custom API base URL (e.g. LiteLLM proxy) |
| `GITHUB_APP_ID` | *(empty)* | GitHub App numeric ID for bug report submission |
| `GITHUB_APP_INSTALLATION_ID` | *(empty)* | GitHub App installation ID |
| `GITHUB_APP_PRIVATE_KEY` | *(empty)* | GitHub App PEM private key (use `\n` for newlines) |
| `GITHUB_TOKEN` | *(empty)* | PAT fallback if App credentials are not set |
| `TAXONOMY_IMPORT_MAX_BYTES` | `5242880` | Size limit (bytes) for the admin taxonomy JSON import |
| `REMINDER_BASE_URL` | *(from `SCD_HOSTNAME`)* | Absolute site root for links inside reminder emails; required for scheduled sends |
| `REMINDER_MIN_INTERVAL_HOURS` | `20` | Skip a recipient reminded within this many hours; `0` disables the guard |
| `REMINDER_STALE_DAYS` | `14` | Days without an entry before somebody counts as overdue |
| `REMINDER_BATCH_SIZE` | `200` | Upper bound on recipients per send |
| `REMINDER_SCHEDULER_ENABLED` | `0` | `1` runs the scheduler in-process instead of from cron |
| `REMINDER_SCHEDULER_INTERVAL` | `60` | Seconds between in-process scheduler ticks (min 15) |
| `SCD_CONFIG_DIR` | `config/` | Directory holding the YAML configuration files |
| `OIDC_OP_DISCOVERY_ENDPOINT` | *(empty)* | OIDC provider discovery URL; enables SSO button when set |
| `OIDC_RP_CLIENT_ID` | *(empty)* | OIDC client ID |
| `OIDC_RP_CLIENT_SECRET` | *(empty)* | OIDC client secret |

---

## API

The REST API supports entry creation with Bearer token authentication. Tokens are managed from the user profile page (`/profile/`).

### Create an entry

```
POST /api/entries/
Authorization: Bearer <token>
Content-Type: application/json
```

```json
{
  "title": "My work this week",
  "description": "Markdown description of work done.",
  "project": "mu2e-daq",
  "category": "software-development",
  "period_kind": "week",
  "period_start": "2026-06-02",
  "period_end": "2026-06-08"
}
```

See `/api/` in the application for full request/response documentation and example scripts. Standalone submission scripts are available in `scripts/`:

- `scripts/scd-post-entry-json` — Python script (reads JSON from file or stdin)
- `scripts/scd-post-entry-json.sh` — Bash/curl equivalent

---

## Management Commands

### `seed_admin`

Creates the initial administrator account. Idempotent — safe to run on every deployment.

```bash
python manage.py seed_admin
```

Reads `SCD_INITIAL_ADMIN_USERNAME`, `SCD_INITIAL_ADMIN_EMAIL`, and `SCD_INITIAL_ADMIN_PASSWORD` from the environment.

### `seed_taxonomy`

Seeds the default set of projects and categories. Idempotent — safe to run repeatedly.

```bash
python manage.py seed_taxonomy
```

Additional projects, categories, and organisational groups can be created through the web interface at `/taxonomy/` (Admin role required).

### `send_reminders`

Fires every reminder schedule that has come due. Safe to run concurrently with
the web pods and with itself — due schedules are claimed atomically.

```bash
python manage.py send_reminders             # normal cron invocation
python manage.py send_reminders --list      # what exists, and what is due
python manage.py send_reminders --dry-run   # rehearse, change nothing
```

See [Email Reminders](#email-reminders) and `docs/man/scd-send-reminders.1`.
