# Docker Deployment Guide

This guide covers deploying SCD Effort Reporting using Docker Compose with a Caddy reverse proxy that handles TLS automatically.

## Contents

- [Prerequisites](#prerequisites)
- [Architecture](#architecture)
- [First-Time Setup](#first-time-setup)
- [Environment File Reference](#environment-file-reference)
- [Starting and Stopping](#starting-and-stopping)
- [Seeding Initial Data](#seeding-initial-data)
- [Updating the Application](#updating-the-application)
- [Backups and Restore](#backups-and-restore)
- [Logs](#logs)
- [Scaling](#scaling)
- [Caddy and TLS](#caddy-and-tls)
- [Troubleshooting](#troubleshooting)

---

## Prerequisites

- Docker Engine 24+ and Docker Compose v2
- A Linux host with ports 80 and 443 reachable from the internet (for automatic TLS)
- A DNS A record pointing your hostname to the server's public IP before first start

Verify your installation:

```bash
docker --version        # Docker version 24.x or later
docker compose version  # Docker Compose version v2.x or later
```

---

## Architecture

```
Internet
    │  HTTPS (443) / HTTP (80)
    ▼
┌─────────┐
│  Caddy  │  Automatic TLS, static file serving, reverse proxy
└────┬────┘
     │  HTTP (8000, internal)
     ▼
┌─────────┐
│   web   │  Django + Gunicorn, multi-stage build (Node → Python)
└────┬────┘
     │  PostgreSQL (5432, internal)
     ▼
┌─────────┐
│   db    │  PostgreSQL 16
└─────────┘
```

Named volumes:

| Volume | Contents |
|---|---|
| `pgdata` | PostgreSQL data directory |
| `staticfiles` | Django collected static files (shared between web and caddy) |
| `media` | User-uploaded media files |
| `caddy_data` | Caddy TLS certificates and state |
| `caddy_config` | Caddy runtime configuration |

---

## First-Time Setup

### 1. Clone the repository

```bash
git clone https://github.com/your-org/SCD-Reporting.git
cd SCD-Reporting
```

### 2. Create the environment file

```bash
cp .env.example .env
```

Open `.env` in an editor and set **at minimum** these three values:

```dotenv
POSTGRES_PASSWORD=<strong-random-password>
DJANGO_SECRET_KEY=<50-character-random-string>
SCD_INITIAL_ADMIN_PASSWORD=<admin-password>
```

Generate a suitable secret key:

```bash
python3 -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

Or without Python:

```bash
openssl rand -base64 50
```

Also set:

```dotenv
DJANGO_ALLOWED_HOSTS=scd-reporting.fnal.gov,localhost
SCD_HOSTNAME=scd-reporting.fnal.gov
SCD_INITIAL_ADMIN_EMAIL=your-email@fnal.gov
DATABASE_URL=postgres://scd:<POSTGRES_PASSWORD>@db:5432/scd
```

> **Security note:** `.env` contains secrets. It is listed in `.gitignore` and must never be committed to version control.

### 3. Build and start

```bash
docker compose up -d --build
```

The first build takes a few minutes because it:
- Installs Node.js dependencies and compiles Tailwind CSS
- Installs all Python dependencies (pure Python — no system libraries required)
- On first start, the `web` container automatically runs `migrate`, `collectstatic`, and `seed_admin`

Check that all three services are healthy:

```bash
docker compose ps
```

Expected output:

```
NAME                     STATUS          PORTS
scd-reporting-db-1       running (healthy)
scd-reporting-web-1      running
scd-reporting-caddy-1    running         0.0.0.0:80->80/tcp, 0.0.0.0:443->443/tcp
```

### 4. Seed the taxonomy

The default projects and categories are not seeded automatically on startup. Run once after the first deploy:

```bash
docker compose exec web python manage.py seed_taxonomy
```

### 5. Verify

Navigate to `https://your-hostname/` in a browser. You should see the login page. Log in with the admin credentials from your `.env` file.

---

## Environment File Reference

Full `.env` reference:

```dotenv
# ── Database ───────────────────────────────────────────────────────────────
POSTGRES_DB=scd
POSTGRES_USER=scd
POSTGRES_PASSWORD=<required — strong random password>

# Used by Django to connect to the database (must match above)
DATABASE_URL=postgres://scd:<POSTGRES_PASSWORD>@db:5432/scd

# ── Django ─────────────────────────────────────────────────────────────────
DJANGO_SETTINGS_MODULE=scd_reporting.settings.prod
DJANGO_SECRET_KEY=<required — 50+ character random string>
DJANGO_ALLOWED_HOSTS=scd-reporting.fnal.gov,localhost

# ── Gunicorn ───────────────────────────────────────────────────────────────
# Rule of thumb: (2 × CPU cores) + 1
GUNICORN_WORKERS=3

# ── Caddy ──────────────────────────────────────────────────────────────────
# Set to a real domain for automatic HTTPS; use "localhost" for HTTP-only
SCD_HOSTNAME=scd-reporting.fnal.gov

# ── Bootstrap admin ────────────────────────────────────────────────────────
SCD_INITIAL_ADMIN_USERNAME=scd-admin
SCD_INITIAL_ADMIN_EMAIL=scd-admin@fnal.gov
SCD_INITIAL_ADMIN_PASSWORD=<required — admin login password>

# ── Authentication ─────────────────────────────────────────────────────────
# Set to 1 once CILogon SSO is configured to prevent local account creation
SCD_DISABLE_LOCAL_SIGNUP=0

# Email verification: none | optional | mandatory
ACCOUNT_EMAIL_VERIFICATION=optional

# ── AI Summary (optional) ───────────────────────────────────────────────────
# Required to enable the AI Summary feature on the Reports page.
# Obtain a key from https://console.anthropic.com
ANTHROPIC_API_KEY=

# Override the model used for summaries (default: claude-sonnet-5)
# ANTHROPIC_SUMMARY_MODEL=claude-sonnet-5
```

---

## Starting and Stopping

```bash
# Start all services in the background
docker compose up -d

# Stop all services (data is preserved in volumes)
docker compose down

# Stop and remove volumes (DESTROYS ALL DATA — use only to reset a test instance)
docker compose down -v

# Restart a single service
docker compose restart web

# View running service status
docker compose ps
```

---

## Seeding Initial Data

The `seed_admin` command runs automatically on every container start (it is idempotent). The taxonomy seed must be run manually once:

```bash
docker compose exec web python manage.py seed_taxonomy
```

To reset the taxonomy to defaults (only if you have not modified it through the UI):

```bash
# This only adds missing records — it does not remove custom entries
docker compose exec web python manage.py seed_taxonomy
```

---

## Updating the Application

```bash
# Pull latest code
git pull

# Rebuild the web image and restart (database is preserved)
docker compose up -d --build web

# Check that migrations ran successfully
docker compose logs web | grep -i migrat
```

The container entrypoint always runs `migrate --noinput` on startup, so migrations are applied automatically on update.

If a deployment requires a taxonomy change, run:

```bash
docker compose exec web python manage.py seed_taxonomy
```

---

## Backups and Restore

### Backup the database

```bash
# Dump to a compressed file
docker compose exec db pg_dump -U scd scd | gzip > scd_backup_$(date +%Y%m%d_%H%M%S).sql.gz
```

Schedule this with cron for regular backups:

```cron
0 3 * * * cd /opt/scd-reporting && docker compose exec -T db pg_dump -U scd scd | gzip > /backups/scd_$(date +\%Y\%m\%d).sql.gz
```

### Restore the database

```bash
# Stop the web service to prevent writes during restore
docker compose stop web

# Restore (drop existing database first if re-initialising)
gunzip -c scd_backup_20260101_030000.sql.gz | docker compose exec -T db psql -U scd scd

# Restart
docker compose start web
```

### Backup media files

```bash
# Copy the media volume contents to a local directory
docker run --rm \
  -v scd-reporting_media:/source:ro \
  -v $(pwd)/media_backup:/dest \
  alpine cp -r /source/. /dest/
```

---

## Logs

```bash
# Follow all service logs
docker compose logs -f

# Follow logs for a single service
docker compose logs -f web
docker compose logs -f caddy
docker compose logs -f db

# Last 100 lines from web
docker compose logs --tail=100 web
```

Django application logs (errors, warnings) go to stdout and are captured by Docker's logging driver, visible via `docker compose logs web`.

---

## Scaling

To handle higher load, increase Gunicorn workers (vertical scaling within a single container):

```dotenv
# In .env
GUNICORN_WORKERS=7
```

Then restart the web service:

```bash
docker compose restart web
```

The rule of thumb for Gunicorn workers is `(2 × CPU cores) + 1`.

For horizontal scaling (multiple web containers behind a load balancer), you would need to:
1. Move sessions to the database or Redis (`django.contrib.sessions.backends.db` is already the default)
2. Point `STATIC_URL` and `MEDIA_URL` to a shared object store or NFS volume
3. Run Caddy in front of multiple web replicas

---

## Caddy and TLS

Caddy obtains and renews TLS certificates automatically from Let's Encrypt when `SCD_HOSTNAME` is set to a real domain with a valid DNS A record pointing to the server.

**Requirements for automatic TLS:**
- Port 80 and 443 must be accessible from the internet
- The DNS A record must resolve before Caddy starts for the first time
- Let's Encrypt rate limits apply (5 certificates per registered domain per week)

**For local or internal deployments** (no public DNS), set:

```dotenv
SCD_HOSTNAME=localhost
```

Caddy will serve HTTP only on port 80, with no TLS. Alternatively, to use a self-signed certificate for an internal hostname, modify `docker/caddy/Caddyfile`:

```
scd-reporting.internal {
    tls internal
    file_server /static/* { root /srv }
    file_server /media/* { root /srv }
    reverse_proxy web:8000
}
```

The `Caddyfile` is mounted read-only from `docker/caddy/Caddyfile`. Edit that file and run `docker compose restart caddy` to apply changes.

---

## Troubleshooting

### Container fails to start

```bash
# Check the full startup log
docker compose logs web
```

Common causes:
- `POSTGRES_PASSWORD` mismatch between `db` and `web` — check `DATABASE_URL` in `.env`
- Database not yet healthy when `web` starts — the `depends_on: condition: service_healthy` check should handle this, but a very slow host may need `pg_isready` retries increased in `compose.yaml`
- Missing `DJANGO_SECRET_KEY` — Django will raise `ImproperlyConfigured`

### Migrations fail

```bash
docker compose exec web python manage.py showmigrations
docker compose exec web python manage.py migrate --verbosity 2
```

### Static files not loading (404)

```bash
# Re-run collectstatic manually
docker compose exec web python manage.py collectstatic --noinput

# Confirm the volume is mounted by caddy
docker compose exec caddy ls /srv/static
```

### Reset everything (development / test only)

```bash
# WARNING: destroys all data
docker compose down -v
docker compose up -d --build
docker compose exec web python manage.py seed_taxonomy
```

### Run a Django shell inside the container

```bash
docker compose exec web python manage.py shell
```

### Run the test suite inside the container

The production image does not include dev dependencies. Use a local virtual environment for tests, or add a dev-stage to the Dockerfile:

```bash
# From a local venv
.venv/bin/pytest tests/
```
