# Deployment Guide

SCD Activity Reporting can run as either a singal Docker container for simple and test installations, or as three Docker containers managed by Docker Compose for more complex installs:

| Service | Image | Role |
|---|---|---|
|`simple` | build from `docker/web/Dockerfile` | Django + Gunicorn + sqlite |
|---|---|---|
| `web` | built from `docker/web/Dockerfile` | Django + Gunicorn |
| `db` | `postgres:16-alpine` | PostgreSQL database |
| `caddy` | `caddy:2-alpine` | HTTPS reverse proxy, static files |

The simple version combines everything that is needed into a single container.  It uses a sqlite (file based) database instead of a separate Postgres database.  The simple version is the same as the main web container, except that it uses a different configuration set to turn on/off features.  

The other point to note is that containers are made as dual architecture so that they can run on both a mac laptop (apple silicon arm64) and on our OKD cluster which is x86.

---

## Prerequisites

- **Docker Engine 24+** or **Docker Desktop** (includes Compose v2)
- A domain name with an A record pointing to your server (for production TLS) [This is setup by our OKD cluster]
- Outbound internet access from the server (Caddy fetches Let's Encrypt certificates) [This is setup by our OKD cluster]

Verify your installation:

```bash
docker --version       # Docker version 24+ or 25+
docker compose version # Docker Compose version v2.x
```

---

## First-time setup

This setup assumes you start with nothing and are going to clone the repo and bootstrap from there.

### 1. Clone the repository

```bash
git clone https://github.com/fermitools/SCD-Reporting.git
cd SCD-Reporting
```

### 2. Create the environment file

```bash
cp .env.example .env
```

Edit `.env` and fill in **at minimum** these values:

```dotenv
# A long random string — generate with: python -c "import secrets; print(secrets.token_hex(32))"
DJANGO_SECRET_KEY=<50+ random characters>

# Your server's public hostname (no https://)
SCD_HOSTNAME=scd-reporting.fnal.gov
DJANGO_ALLOWED_HOSTS=scd-reporting.fnal.gov

# Database credentials (chosen by you — Compose creates the DB on first run)
POSTGRES_PASSWORD=<strong random password>

# Password for the initial scd-admin account
SCD_INITIAL_ADMIN_PASSWORD=<strong password>
```

> **SQLite note:** For a quick local test you can omit `POSTGRES_*` variables and
> comment out the `db` and `caddy` services. SQLite is the default when
> `DATABASE_URL` is not set.

### 3. (Optional) Configure OIDC or Google SSO

Authentication is handled via a number of different supported methods.  For testing
it is important that you have AT LEAST one version that will get you into the admin 
role in Django.  For the very first time you spin up the application, you will NEED 
this to be password based so that you can login as admin and then promote another 
account to admin level (i.e. an account that is authenticated via a SSO or other 
provider chain)

After that you will want to enable some form of Single signon or identity provider chain.
To do this....

See [HOWTO-SSO.md](HOWTO-SSO.md) for full details. Add the relevant variables to
`.env` — No code changes are needed to enable/disable an identity provider.  You 
just need to set or leave blank the entries that correspond to their secrets and urls

When the application loads, it will look for the `.env` file.  If you want your secrets coming
directly from the `.env` file then it needs to be visible inside the container at runtime.
In this case to access an OIDC client secret stored in a file, you will have to mount the secrets directory into the
`web` container and point `OIDC_CLIENT_SECRET_FILE` at it:

```yaml
# compose.override.yaml — do not edit compose.yaml
services:
  web:
    volumes:
      - /etc/scd/secrets:/run/secrets:ro
```

```dotenv
# .env
OIDC_CLIENT_SECRET_FILE=/run/secrets/oidc_secret
```
Which makes it vissible to the container image.  Alternatives are to pass secrets as environment variables
during startup of the application.

### 4. Build and start

There are three ways to run the application:

- **Direct / local webserver** — best for development; runs Django's dev server directly on your machine
- **Local containerised webserver** — runs the full Docker Compose stack (web + postgres + caddy) locally
- **OKD deployment** — production deployment via Helm on the Fermilab OKD cluster

---

#### Option A: Direct / local webserver (development)

`scripts/start-scd-reporting` creates the virtual environment, installs/updates all dependencies, runs migrations, seeds initial data, and starts the Django development server in the background.

**First run (sets the admin password):**

```bash
./scripts/start-scd-reporting --admin-password yourpassword
```

**Subsequent runs (no password needed — account already exists):**

```bash
./scripts/start-scd-reporting
```

If for some reason you can't remember what you set the admin password to you can change
it via the commandline for the locally running server:

```
> python manage.py changepassword scd-admin
Changing password for user 'scd-admin@fnal.gov'
Password: 
Password (again): 
Password changed successfully for user 'scd-admin@fnal.gov'
```

The server starts at <http://127.0.0.1:8000> and logs to `scripts/logs/scd-reporting.log`.  You should be able to login
with the scd-admin account.  From the main page enter the `scd-admin@fnal.gov` email and the password you set.

[Alt]
Altneratively you can use the Django admin url to login directly to the backend.  The endpoint for this (assuming you spun it up on port 8000) is:
```
http://localhost:8000/admin/
```
Here you login as `scd-admin` and use your password that you set.

Congrats you now have a working application!

**Common options:**

When starting up using the scripts you can pass a number of common options.  These will
override anything that is in the config files or passed through the .env file.  (See the manpages for details)

| Option | Description |
|---|---|
| `--admin-password <pass>` | Set/reset the initial admin password (first run) |
| `--anthropic-key <key>` | Enable the AI Summary feature |
| `--oidc-provider-url <url>` | OIDC discovery URL (enables SSO button) |
| `--oidc-client-id <id>` | OIDC client ID |
| `--oidc-secret-file <path>` | Path to a file containing the OIDC client secret |
| `--port <port>` | Port to listen on (default: `8000`) |
| `--prod` | Bind to `0.0.0.0`, use production settings |
| `--no-update` | Skip pip/npm dependency update checks |
| `--with-tailwind` | Also start the Tailwind CSS watcher (hot-reload CSS) |
| `--tail` | Tail the server log after starting |

All options can also be set via environment variables or a `.env` file in the project root — see the script header for the mapping.

**Stop the server:**

To stop the locally running server use:

```bash
./scripts/stop-scd-reporting
```

Pass `--tail` to print the last 20 log lines before stopping:

```bash
./scripts/stop-scd-reporting --tail
```

This is useful to see a "post-mortem" of what might have gone wrong if things get stuck.

NOTE:

When using the local "live" version of the application you can make code changes to the html or underlying
database models and they will be instantantly reflected in the application.  This is useful if you are doing interface
modification or changing parts of the schema since you don't have to rebuild the whole project.  It's MUCH faster than
doing development against the docker-ized version or the deployed OKD version.

---

#### Option B: Local Docker Compose (containerised)

If you want to test in a more "production-like" environment, then you want to build out the 
docker container and run that.  This is important because it builds the image and embeds 
all the files, mounts areas etc... Basically it gets the application ready to run some
place other than your local development environment (i.e. not on Andrew's Laptop)

First you build the image using the `docker compose` syntax:

```bash
docker compose up -d --build
```

On the very first run this will:
1. Pull `postgres:16-alpine` and `caddy:2-alpine`
2. Build the `web` image (compiles Tailwind CSS, installs Python dependencies)
3. Start the database, run Django migrations, seed the initial admin account
4. Obtain a TLS certificate from Let's Encrypt (production only)

Watch the startup logs:

```bash
docker compose logs -f web
```

You should see:

```
[entrypoint] Running database migrations...
[entrypoint] Collecting static files...
[entrypoint] Seeding initial admin account...
[entrypoint] Starting gunicorn...
```

You can also run the `start-docker` and `stop-docker` scripts to fire up
the application from docker.  In this case it will need to bind to some ports
on your local machine so make sure that these are free before firing it up (or map 
the ports to a range that you aren't using). Using scripts will ensure that
options are passed correctly.

*IF* you use Docker-desktop you can also launch from there.

---

#### Option C: OKD / Helm (production)

The final deployment option is to fully deploy to the Fermilab OKD 
cluser.  For this you will need to be added to the project 
on the OKD cluster.  You will also need to login etc....

In general see the [OKD Deployment](#okd--helm-deployment) section below for details on
how this all works and the helper scripts.

---

### 5. Verify

Once you have a running instance of the application you will want to check it.  Open up your
web browser (firefox or chrome work well) and go to the appropriate URL:

Open `http://127.0.0.1:8000` (local) or `https://scd-reporting.fnal.gov` (Docker/OKD) in your browser. Sign in with:

If you don't have any other accounts created you will login as the admin.

- **Username:** `scd-admin`
- **Password:** the value you passed to `--admin-password` or `SCD_INITIAL_ADMIN_PASSWORD`

For Docker Compose, check that all services are healthy:

```bash
docker compose ps
```

All services should show `(healthy)` or `Up`.

---

## Updating

Now for the fun part.  You want to make some changes...how do you do it and push it out?
First off there are a couple of different layers.
* Web application (Django/python logic)
* Webpage Templates (CSS w/ Tailwind)
* Configuration parameters
* Backend database/data store

To update your code base you will do a pull from the repository and then you will
need to rebuild the container image.  This is basically a git command followed by a 
`docker compose`

```bash
git pull
docker compose up -d --build
```

Compose rebuilds only the `web` image, pulls any updated base images, and
restarts the changed services. Migrations run automatically on the next container
start.

To force a full rebuild without the Docker layer cache (after a Python dependency
change, for example) you need to rebuild and then update:

```bash
docker compose build --no-cache web
docker compose up -d
```

This is true if you are working with the web application or the webpage/templates

If you are working with configuration or secrets that is done separately.

---

## Secrets management

There are a lot of *secrets* which are needed for an application like 
this and we don't want to expose them, but we do want to pass them 
to the application and to have them active at run time.  This means that
we have to be careful about how we work with them.

Best practice is to just never have them written down anywhere 
durable and pass them to the program on startup.  That's not practical 
when it comes to API keys and the like this is where we have 
managed secrets and need to be careful about what gets committed to GitHub.

### Avoid putting secrets directly in `.env` when possible

The `.env` file is a really great way to set a whole bunch of 
environment variables that we need at runtime.  The problem is that
if it were to get committed to the GitHub repo, that would be bad 
since people could see the secrets that are in it.

That being said....for local testing the `.env` file is great.
There is a skeleton for this that has all the fields that I typically
use.  Copy the example file to `.env` and fill out the fields 
with the right values.  Then you are off to the races and can
spin up with different services enabled that you wouldn't 
want in production (like for example Google Sign in
so you can test different account setups without relying on the fermi SSO, 
or to get a couple of different accounts setup so you can do cross user
testing)

But there are better ways to do this...

So for the OIDC client secret (the Fermi SSO), the preferred pattern is a secret file:

```bash
sudo mkdir -p /etc/scd/secrets
echo "your-client-secret" | sudo tee /etc/scd/secrets/oidc_secret
sudo chmod 600 /etc/scd/secrets/oidc_secret
```

Then in `compose.override.yaml`:

```yaml
services:
  web:
    volumes:
      - /etc/scd/secrets:/run/secrets:ro
```

And in `.env`:

```dotenv
OIDC_CLIENT_SECRET_FILE=/run/secrets/oidc_secret
```

Slick right?  Basically you put the reference in the `.env` file which makes 
startup ease, and the actual value in a different place which can be read
at run time.

### Fetching secrets from HashiCorp Vault

Better still: don't ship the secrets with the deployment at all. Give the pod a
short-lived Vault token and let it fetch its own secrets at startup.

`scripts/get-vault-apptoken.sh` produces that token. Your personal LDAP login to
Vault is long-lived, so the script only performs it when you don't already have
a valid session — otherwise it goes straight to work and prompts for nothing. It
then reads the AppRole `role_id`, generates a fresh `secret_id`, exchanges the
pair for an application token, and verifies the result:

```bash
# Just print a token
./scripts/get-vault-apptoken.sh

# Load it into the current shell
eval "$(./scripts/get-vault-apptoken.sh --format env)"

# Confirm the token can actually read the app's secrets
./scripts/get-vault-apptoken.sh --check-path secret/scd-reporting
```

To deploy with it, add `--vault` and the deploy script does the whole dance —
fetch the token, write a temporary 0600 Helm values fragment, pass it to
`helm upgrade`, and delete it on exit (including on Ctrl-C):

```bash
./scripts/deploy.sh -f my-values.yaml --vault
```

That sets `VAULT_ADDR`, `VAULT_APPROLE`, `VAULT_APPROLE_MOUNT`, `VAULT_SECRET_PATH`
and `VAULT_TOKEN` in the pod's environment. The vault values are empty by
default, so deployments that don't pass `--vault` are unaffected.

The application token is **short-lived**. It is meant to be consumed at startup,
not to sit in a file — once it expires the pod needs a redeploy with a fresh
one. Never put a real `vault.token` in `my-values.yaml` or any committed file.

### How the application consumes the secrets

`scd_reporting/vault.py` runs at the top of `settings/base.py`, before any
setting reads the environment. It fetches each section and injects the values as
environment variables, so every existing `os.environ.get(...)` in the settings
keeps working unchanged — nothing downstream knows Vault is involved.

The layout is one KV v2 secret per service beneath a per-environment prefix:

| Vault path (relative to `VAULT_SECRET_PATH`) | key | environment variable |
|---|---|---|
| `anthropic` | `api_key` | `ANTHROPIC_API_KEY` |
| `django` | `secret_key` | `DJANGO_SECRET_KEY` |
| `django` | `initial_admin_password` | `SCD_INITIAL_ADMIN_PASSWORD` |
| `email` | `password` | `EMAIL_HOST_PASSWORD` |
| `google` | `client_secret` | `GOOGLE_CLIENT_SECRET` |
| `oidc` | `client_secret` | `OIDC_CLIENT_SECRET` |
| `postgres` | `password` | `POSTGRES_PASSWORD` |

`VAULT_SECRET_PATH` selects the environment — `okd/shared/prod/scd-reporting` or
`okd/shared/test/scd-reporting`. `deploy.sh` defaults to the prod path; override
with `--vault-path` or `$SCD_VAULT_PATH`:

```bash
./scripts/deploy.sh -f my-values.yaml --vault --vault-path okd/shared/test/scd-reporting
```

Sections that don't exist are skipped, so a partially populated environment
still boots. The `github` section is part of the shared house layout but is not
populated here; GitHub credentials continue to come from the Helm secret.

**Precedence: an environment variable already set to a non-empty value wins over
Vault.** This keeps the project-wide ordering (command line > environment >
config source > defaults) and means a value pinned in `my-values.yaml` still
overrides Vault. The chart ships those keys as empty strings, which count as
unset, so in a normal deploy Vault fills them.

#### Postgres

The password is in Vault but `DATABASE_URL` is assembled in the ConfigMap, where
the password isn't available. Put a placeholder in the URL and the loader
substitutes the value (URL-encoding it) after reading Vault:

```yaml
database:
  url: "postgres://scd:${POSTGRES_PASSWORD}@db:5432/scd"
```

A `DATABASE_URL` without the placeholder is left untouched, so the SQLite
default is unaffected.

#### Failure behaviour

If Vault is configured but unreachable, or the token has expired, the pod
**fails to start** rather than booting with an insecure fallback `SECRET_KEY`.
That is the intended behaviour: a Vault failure should be loud. To let the pod
start anyway and fall back to ConfigMap/Secret values, set `vault.optional: "1"`
in your values file.

Because the app token is short-lived, a pod that restarts after the token
expires will crash-loop until you redeploy with a fresh one. Check the pod logs
for `Vault rejected the supplied credentials`.

#### Verifying before you deploy

`scripts/scd-vault-check` walks the same section map the application uses and
reports what would resolve, without starting Django. Values are never printed —
each key shows its length and a short SHA-256 fingerprint, enough to confirm
test and prod really differ or that a rotation took effect.

```bash
export VAULT_ADDR=https://ssivault.fnal.gov:8200
export VAULT_TOKEN=$(vault print token)
./scripts/scd-vault-check --env prod
./scripts/scd-vault-check --env prod --show-missing   # include absent keys
```

Exit status is 0 when every existing section was readable, 1 on a policy
problem, 2 on a usage error and 3 when Vault can't be reached. See
`man/scd-vault-check.1`.

#### If you get `403 permission denied` on the secret-id

That almost always means `~/.vault-token` holds an **application** token instead
of your own login — the last line of the old manual recipe,
`vault login -method=token s.…`, replaces your LDAP session with the app token.
An app token can read its own `role-id` but is not allowed to mint a new
`secret-id`, so Vault returns a 403.

The script now checks for this before it gets that far and re-prompts you to log
in. To confirm what your current session actually is:

```bash
vault token lookup                                    # look at display_name / path
vault token capabilities auth/td-approles/role/scd-mu2e-app/secret-id
```

A personal login shows `display_name  ldap-<user>` and `create`/`update` on that
path; an app token shows `display_name  td-approles` and only `list, read`. Get
your own session back with `vault login -method=ldap`, or just run the script
with `--force-login`. This is also why `--set-local-token` is not the default.

Defaults (server address, role, mount) can be put in `config/vault.yaml` —
copy `config/vault.yaml.example` — and are overridden by environment variables,
which are in turn overridden by command-line flags. Full documentation:

```bash
man ./man/get-vault-apptoken.1
```

### Never commit `.env`

`.env` is listed in `.gitignore`. If you accidentally commit it, rotate all
secrets immediately and rewrite the git history.

This is a pain.  Just don't do commit your `.env`.  Please don't.

The rest of the secrets are handled the same way.  So use this as a pattern and 
the application will start up with the right things active.

---

## Backup and restore

This whole application is designed to be mostly ephemeral.  The only parts that need
to survive restarts are the backend database and the secrets/config.  Everything else 
can be part of the git repo and can be tagged.  In the case of an sqlite deployment the db file
will live in a persisted area on the OKD cluser.  For your local installs it's 
in the top level of the project.  For a postgres deployment, the data lives in
that server.

### Backup the database

```bash
docker compose exec db pg_dump -U scd scd | gzip > scd-$(date +%Y%m%d).sql.gz
```

### Restore from a backup

```bash
# Stop the web container first to avoid writes during restore
docker compose stop web
gunzip -c scd-20260101.sql.gz | docker compose exec -T db psql -U scd scd
docker compose start web
```

### Backup uploaded media files

```bash
docker run --rm \
    -v scd-reporting_media:/data \
    -v $(pwd):/backup \
    alpine tar czf /backup/media-$(date +%Y%m%d).tar.gz -C /data .
```

---

## Routine operations

### View logs

```bash
docker compose logs -f            # all services
docker compose logs -f web        # web only
```

### Run a management command

There are a number of "management commands" that you are going to want
to have access to when you are developing or debugging.  The most common
will be reseting the admin password and clearing portions of the database.
There is a helper script `manage.py` which handles this.  Read the 
instructions, but for the most part it is just:

```bash
docker compose exec web python manage.py <command>
```

For example, to open a Django shell:

```bash
docker compose exec web python manage.py shell
```

### Stop the stack

```bash
docker compose down          # stops containers, keeps volumes
docker compose down -v       # stops containers AND deletes all data volumes
```

---

## Architecture notes

### Static files

`collectstatic` runs automatically on every container start and writes to the
`staticfiles` Docker volume. Caddy serves `/static/*` and `/media/*` directly
from that volume without proxying to Django, so static assets are fast even under
load.

### TLS / HTTPS

Caddy automatically provisions and renews Let's Encrypt TLS certificates for
`SCD_HOSTNAME`. The `caddy_data` volume persists the certificate across restarts.

For local development or an intranet deployment where Let's Encrypt cannot reach
the server, Caddy falls back to a self-signed certificate. You may see a browser
warning — this is expected.

### Health checks

The `web` container exposes a health check that polls `/accounts/login/` every
30 seconds. Caddy will not receive traffic until the `web` container is healthy,
preventing 502 errors during startup or after a restart.

### Non-root container user

The `web` container runs as `appuser` (UID 1001). The `staticfiles` and `media`
volumes are owned by this UID. If you mount host directories in their place,
ensure they are readable and writable by UID 1001.

---

## Environment variable reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `DJANGO_SECRET_KEY` | **Yes** | insecure dev key | Django secret key — must be unique and secret in production |
| `SCD_HOSTNAME` | **Yes** (prod) | `localhost` | Public hostname — used by Caddy and for CSRF trusted origins |
| `DJANGO_ALLOWED_HOSTS` | **Yes** (prod) | `localhost` | Comma-separated hostnames Django will serve |
| `POSTGRES_PASSWORD` | **Yes** (if using Postgres) | — | Postgres password |
| `POSTGRES_USER` | No | `scd` | Postgres username |
| `POSTGRES_DB` | No | `scd` | Postgres database name |
| `SCD_INITIAL_ADMIN_PASSWORD` | **Yes** (first run) | — | Password for the `scd-admin` account created on first start |
| `SCD_INITIAL_ADMIN_USERNAME` | No | `scd-admin` | Username for the initial admin account |
| `SCD_INITIAL_ADMIN_EMAIL` | No | `scd-admin@fnal.gov` | Email for the initial admin account |
| `GUNICORN_WORKERS` | No | `3` | Number of Gunicorn worker processes |
| `GUNICORN_LOG_LEVEL` | No | `info` | Gunicorn log verbosity (`debug`, `info`, `warning`, `error`) |
| `ACCOUNT_EMAIL_VERIFICATION` | No | `optional` | `none` \| `optional` \| `mandatory` |
| `SCD_DISABLE_LOCAL_SIGNUP` | No | `0` | Set to `1` to block new local account creation |
| `ANTHROPIC_API_KEY` | No | — | Enables AI-generated report summaries |
| `OIDC_PROVIDER_URL` | No | — | OIDC discovery URL — see [HOWTO-SSO.md](HOWTO-SSO.md) |
| `OIDC_CLIENT_ID` | No | — | OIDC client ID |
| `OIDC_CLIENT_SECRET_FILE` | No | — | Path to file containing the OIDC client secret |
| `OIDC_CLIENT_SECRET` | No | — | OIDC client secret as env var (file takes precedence) |
| `GOOGLE_CLIENT_ID` | No | — | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | No | — | Google OAuth client secret |
| `CSRF_TRUSTED_ORIGINS` | No | derived from `SCD_HOSTNAME` | Comma-separated trusted origins for CSRF (e.g. `https://scd.example.org`) |

---

## Troubleshooting

### Container exits immediately after starting

```bash
docker compose logs web
```

Common causes:
- `POSTGRES_PASSWORD` not set — Postgres refuses connections
- `DJANGO_SECRET_KEY` is the insecure default in `DEBUG=False` mode (prod settings enforce this)
- A migration failed — check for schema conflicts

### `502 Bad Gateway` from Caddy

The `web` container is not yet healthy. Check its status:

```bash
docker compose ps web
docker compose logs web
```

### Database connection refused

Ensure the `db` container is running and healthy before `web` starts. Compose
enforces this via `depends_on: condition: service_healthy`.

If you upgraded Postgres and the data volume has an older major version:

```bash
docker compose down
docker volume rm scd-reporting_pgdata   # destroys data — restore from backup first!
docker compose up -d
```

### "CSRF verification failed"

Ensure `SCD_HOSTNAME` in `.env` matches the hostname in the browser URL exactly.
The `CSRF_TRUSTED_ORIGINS` setting is derived from `SCD_HOSTNAME` automatically.
