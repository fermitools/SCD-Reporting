# SCD Reporting — Django Admin Cheat Sheet

## Django Admin UI

**URL:** `/admin/`  
**Superusers:** `scd-admin` · `norman` (both use `scd-admin@fnal.gov`)

### Reset a password

```bash
python manage.py changepassword <username>
```

Or in the admin UI: **Users → select user → PASSWORD section → "this form"** link.

---

## User Management

### Roles

| Role | Key | Access |
|------|-----|--------|
| User | `user` | Submit / view own entries |
| Functional Lead | `functional_lead` | Leads one or more projects |
| Group Leader | `group_leader` | Leads a work group |
| Division Head | `division_head` | Oversees multiple groups |
| Auditor | `auditor` | Read-only access to all reports |
| Administrator | `admin` | Full access + Django admin |

### Create a superuser (interactive)

```bash
python manage.py createsuperuser
```

### Seed the initial admin user (non-interactive)

```bash
SCD_INITIAL_ADMIN_USERNAME=scd-admin \
SCD_INITIAL_ADMIN_EMAIL=scd-admin@fnal.gov \
SCD_INITIAL_ADMIN_PASSWORD=changeme \
python manage.py seed_admin
```

The command is idempotent — safe to run again if the user already exists.

### Edit a user in the shell

```python
python manage.py shell

from apps.accounts.models import User
u = User.objects.get(email='someone@fnal.gov')
u.role = User.Role.AUDITOR   # see role table above for valid values
u.display_name = 'Jane Smith'
u.employee_id = '12345'
u.save()
```

### List all users and roles

```python
User.objects.values('email', 'role', 'is_active').order_by('email')
```

### Deactivate / reactivate a user

```python
u = User.objects.get(email='someone@fnal.gov')
u.is_active = False   # True to reactivate
u.save()
```

### Assign a user to a work group

```python
from apps.taxonomy.models import WorkGroup
u.group = WorkGroup.objects.get(name='SCD Computing')
u.save()
```

---

## Taxonomy Management

Taxonomy records (projects, categories, groups, lab priorities) are managed at `/admin/` or via the shell.

### Seed default taxonomy (idempotent)

```bash
python manage.py seed_taxonomy
```

Seeds: DUNE, NOvA, MicroBooNE, CMS, LSST · Scientific, Operations, Outreach, Training · Lab Priorities.

### Add / deactivate a project

```python
from apps.taxonomy.models import Project
Project.objects.create(name='My Project', short_code='MYPROJ', sort_order=60)

# Deactivate without deleting
Project.objects.filter(name='My Project').update(is_active=False)
```

Same pattern works for `Category`, `WorkGroup`, and `LabPriority`.

---

## Database

### Run migrations

```bash
python manage.py migrate
```

### Check for unapplied migrations

```bash
python manage.py showmigrations
```

### Open the database shell

```bash
python manage.py dbshell
```

### Backup (SQLite)

```bash
cp db.sqlite3 db.sqlite3.bak
```

### Dump / load data

```bash
python manage.py dumpdata --indent 2 > backup.json
python manage.py loaddata backup.json
```

---

## Site Settings

### Toggle open signup (allow anyone to register)

```python
from apps.accounts.models import SiteSettings
s = SiteSettings.get_solo()
s.allow_signup = True   # False to close
s.save()
```

Or set env var `SCD_DISABLE_LOCAL_SIGNUP=1` to disable local signup entirely regardless of the setting above.

---

## AI Prompt Configuration

The global AI summary prompt is at `/admin/reports/aipromptconfig/`.  
Users can also configure personal prompts from the dashboard.

- `system_prompt` — instructions given to the model
- `user_template` — must contain `{entries}` as the only placeholder

### Reset to defaults in the shell

```python
from apps.reports.models import AIPromptConfig, DEFAULT_SYSTEM, DEFAULT_USER_TMPL
config = AIPromptConfig.get_solo()
config.system_prompt = DEFAULT_SYSTEM
config.user_template = DEFAULT_USER_TMPL
config.save()
```

---

## Key Environment Variables

### Required in production

| Variable | Purpose |
|----------|---------|
| `DJANGO_SECRET_KEY` | Django signing key — must be strong and unique |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated list of allowed hostnames |
| `DATABASE_URL` | PostgreSQL/SQLite connection string |

### Optional but commonly set

| Variable | Default | Purpose |
|----------|---------|---------|
| `DJANGO_SETTINGS_MODULE` | `scd_reporting.settings.dev` | Switch to `scd_reporting.settings.prod` in production |
| `SCD_HOSTNAME` | — | Derives `CSRF_TRUSTED_ORIGINS` automatically |
| `CSRF_TRUSTED_ORIGINS` | — | Explicit CSRF origins (overrides `SCD_HOSTNAME`) |
| `SCD_DISABLE_LOCAL_SIGNUP` | `0` | Set `1` to disable local email/password signup |
| `ACCOUNT_EMAIL_VERIFICATION` | `optional` | `mandatory` / `optional` / `none` |
| `ANTHROPIC_API_KEY` | — | Enables AI report summaries |
| `ANTHROPIC_SUMMARY_MODEL` | `claude-sonnet-5` | Claude model to use for summaries |
| `EMAIL_HOST` | — | SMTP host; omit to log emails to console |
| `EMAIL_PORT` | `587` | `587` = STARTTLS, `465` = implicit SSL |
| `EMAIL_HOST_USER` | — | SMTP username |
| `EMAIL_HOST_PASSWORD` | — | SMTP password |
| `DEFAULT_FROM_EMAIL` | `noreply@localhost` | Sender address |
| `MFA_WEBAUTHN_ALLOW_INSECURE_ORIGIN` | `false` (prod) | Set `true` for localhost dev only |

### SSO / OIDC (Keycloak)

| Variable | Purpose |
|----------|---------|
| `OIDC_PROVIDER_URL` | Realm base URL or `.well-known` discovery URL |
| `OIDC_CLIENT_ID` | Client ID registered in Keycloak |
| `OIDC_CLIENT_SECRET` | Client secret (or use file below) |
| `OIDC_CLIENT_SECRET_FILE` | Path to file containing the secret (preferred) |

### GitHub bug report integration

| Variable | Purpose |
|----------|---------|
| `GITHUB_APP_ID` | GitHub App numeric ID |
| `GITHUB_APP_INSTALLATION_ID` | Installation ID for the target repo |
| `GITHUB_APP_PRIVATE_KEY` | PEM private key (newlines as `\n` or literal) |
| `GITHUB_TOKEN` | Fallback PAT if App credentials are not set |

---

## Static Files

```bash
python manage.py collectstatic --noinput
```

---

## Common Troubleshooting

| Symptom | Fix |
|---------|-----|
| `ImproperlyConfigured: DJANGO_SECRET_KEY…` | Set a strong `DJANGO_SECRET_KEY` env var in production |
| 403 on admin | User needs `is_staff=True` and `is_superuser=True` |
| AI summary fails | Check `ANTHROPIC_API_KEY` is set; verify prompt template contains `{entries}` |
| Email not sending | Set `EMAIL_HOST`; check `EMAIL_PORT`, credentials |
| SSO not appearing on login page | All three OIDC vars must be set (`URL`, `CLIENT_ID`, `SECRET`) |
| CSRF errors behind proxy | Set `SCD_HOSTNAME` or `CSRF_TRUSTED_ORIGINS` |
| `KeyError` in template | User saved a prompt template with an unsupported placeholder — fix via `/admin/reports/aipromptconfig/` |
