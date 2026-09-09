# SSO / OIDC Integration Guide

This document covers how to configure SCD Effort Reporting to authenticate users
via an external OpenID Connect (OIDC) identity provider, such as Keycloak or CILogon.

---

## Table of Contents

- [How it works](#how-it-works)
- [Prerequisites](#prerequisites)
- [Step 1 — Register the application in your IdP](#step-1--register-the-application-in-your-idp)
- [Step 2 — Store the client secret](#step-2--store-the-client-secret)
- [Step 3 — Set environment variables](#step-3--set-environment-variables)
- [Step 4 — Start the server with SSO enabled](#step-4--start-the-server-with-sso-enabled)
- [Step 5 — Verify the login page](#step-5--verify-the-login-page)
- [Step 6 — Disable local signup (optional)](#step-6--disable-local-signup-optional)
- [OKD / Helm deployment](#okd--helm-deployment)
- [Docker Compose deployment](#docker-compose-deployment)
- [Claim mapping](#claim-mapping)
- [Troubleshooting](#troubleshooting)
- [Environment variable reference](#environment-variable-reference)

---

## How it works

When `OIDC_PROVIDER_URL`, `OIDC_CLIENT_ID`, **and** a client secret
(`OIDC_CLIENT_SECRET_FILE` or `OIDC_CLIENT_SECRET`) are all set, the application:

1. Enables the `allauth` OpenID Connect provider at startup
2. Shows a **"Log in with SSO"** button on the login page
3. Redirects users to your identity provider for authentication
4. Receives the callback, validates the ID token, and creates or updates the local
   user account using the claims from the IdP
5. Logs the user in and redirects to the dashboard

Local email/password login continues to work alongside SSO unless you explicitly
disable it (see [Step 6](#step-6--disable-local-signup-optional)).

---

## Prerequisites

- A registered OIDC client in your identity provider (Keycloak, CILogon, etc.)
- The client's **client ID** and **client secret**
- The IdP's **discovery URL** (the `.well-known/openid-configuration` endpoint or
  the base realm URL)
- The application running over HTTPS in production (required by most IdPs for
  redirect URIs)

---

## Step 1 — Register the application in your IdP

In your identity provider's administration console, create an OIDC client with the
following settings:

| Setting | Value |
|---|---|
| **Client ID** | `scd-report-summarizer` (or your chosen ID) |
| **Client protocol** | `openid-connect` |
| **Access type** | `confidential` (requires a client secret) |
| **Valid redirect URIs** | See table below |
| **Web origins** | Your application's base URL (for CORS, if required) |

### Callback / redirect URIs

Register the following URI in your IdP for each environment where the app runs:

```
https://<your-hostname>/accounts/oidc/keycloak/login/callback/
```

| Environment | Redirect URI |
|---|---|
| Local development | `http://localhost:8000/accounts/oidc/keycloak/login/callback/` |
| Production | `https://your-domain.fnal.gov/accounts/oidc/keycloak/login/callback/` |

> The path segment `keycloak` is the internal `provider_id` used by the
> application. It does not need to match anything in your IdP — it is just a label.

### Fermilab Keycloak (current configuration)

The application is pre-configured for:

| Parameter | Value |
|---|---|
| **Discovery URL** | `https://kc.apps.okddev.fnal.gov/realms/myrealm/.well-known/openid-configuration` |
| **Client ID** | `scd-report-summarizer` |

---

## Step 2 — Store the client secret

The client secret should never be placed directly in a shell profile or committed
to version control. Two options are supported:

### Option A — Secret file (recommended for production)

Write the secret to a file readable only by the application process:

```bash
echo "your-client-secret" > /etc/scd/oidc_secret
chmod 600 /etc/scd/oidc_secret
```

Point the application at this file with `OIDC_CLIENT_SECRET_FILE` (see Step 3).

### Option B — Environment variable (acceptable for local development)

```bash
export OIDC_CLIENT_SECRET="your-client-secret"
```

If both are set, the file takes precedence.

---

## Step 3 — Set environment variables

Three variables are required to enable SSO:

| Variable | Description |
|---|---|
| `OIDC_PROVIDER_URL` | Discovery URL **or** base realm URL of your IdP |
| `OIDC_CLIENT_ID` | The client ID registered in your IdP |
| `OIDC_CLIENT_SECRET_FILE` | Path to the file containing the client secret |

Both URL forms are accepted for `OIDC_PROVIDER_URL`:

```bash
# Full discovery URL (with .well-known suffix)
export OIDC_PROVIDER_URL="https://kc.apps.okddev.fnal.gov/realms/myrealm/.well-known/openid-configuration"

# Or just the base realm URL — either works
export OIDC_PROVIDER_URL="https://kc.apps.okddev.fnal.gov/realms/myrealm"
```

Full example:

```bash
export OIDC_PROVIDER_URL="https://kc.apps.okddev.fnal.gov/realms/myrealm/.well-known/openid-configuration"
export OIDC_CLIENT_ID="scd-report-summarizer"
export OIDC_CLIENT_SECRET_FILE="/etc/scd/oidc_secret"
```

---

## Step 4 — Start the server with SSO enabled

### Using the start script

**macOS / Linux:**

```bash
./start-scd-reporting \
  --oidc-provider-url "https://kc.apps.okddev.fnal.gov/realms/myrealm/.well-known/openid-configuration" \
  --oidc-client-id "scd-report-summarizer" \
  --oidc-secret-file "/etc/scd/oidc_secret"
```

**Windows (PowerShell):**

```powershell
.\start-scd-reporting.ps1 `
  -OidcProviderUrl "https://kc.apps.okddev.fnal.gov/realms/myrealm/.well-known/openid-configuration" `
  -OidcClientId "scd-report-summarizer" `
  -OidcSecretFile "C:\secrets\oidc_secret.txt"
```

### Using environment variables directly

If the environment variables are already exported in your shell (or loaded from a
`.env` file), simply start the server normally:

```bash
source /etc/scd/scd-env.sh   # or however you load your env
./start-scd-reporting
```

### Manual start (without the script)

```bash
source .venv/bin/activate
export OIDC_PROVIDER_URL="..."
export OIDC_CLIENT_ID="scd-report-summarizer"
export OIDC_CLIENT_SECRET_FILE="/etc/scd/oidc_secret"
python manage.py runserver
```

---

## Step 5 — Verify the login page

Navigate to `http://localhost:8000/accounts/login/` (or your production URL).
You should see the local email/password form **and** a **"Log in with SSO"** button
below it.

If the button is not visible, SSO is not enabled. Check that all three environment
variables are set and non-empty, and review the server log (`logs/scd-reporting.log`)
for startup errors.

---

## Step 6 — Disable local signup (optional)

Once SSO is working and users can log in, you may want to prevent new local
accounts from being created:

```bash
export SCD_DISABLE_LOCAL_SIGNUP=1
```

This removes the local signup flow entirely. Existing local accounts (including the
`scd-admin` administrator account) continue to work — only the creation of *new*
local accounts is blocked.

The self-serve signup toggle in the Admin Users page (`/admin-users/`) is also
overridden when this variable is set to `1`.

---

## OKD / Helm deployment

Add the OIDC values to your local `my-values.yaml` override file (never in `values.yaml`):

```yaml
# my-values.yaml
oidc:
  providerUrl: "https://kc.apps.okddev.fnal.gov/realms/myrealm/.well-known/openid-configuration"
  clientId: "scd-report-summarizer"
  clientSecret: "your-client-secret"
```

Then upgrade:

```bash
helm upgrade scd-reporting ./helm/simple -n scd-reporting -f my-values.yaml
```

The secret is stored in the `scd-reporting-secret` Kubernetes `Secret` object and
injected into the pod as `OIDC_CLIENT_SECRET`. A pod restart is not required — Helm
applies the Secret update and the next pod start picks it up automatically.

If the pod is already running and you want to update the secret without a full
Helm upgrade, patch it directly:

```bash
oc set data secret/scd-reporting-secret -n scd-reporting \
  OIDC_CLIENT_SECRET="your-client-secret"
./scripts/restart-pod.sh
```

---

## Docker Compose deployment

Add the OIDC variables to your `.env` file alongside the other production settings:

```dotenv
# .env
DJANGO_SECRET_KEY=...
POSTGRES_PASSWORD=...
SCD_INITIAL_ADMIN_PASSWORD=...
SCD_HOSTNAME=your-domain.fnal.gov

# SSO
OIDC_PROVIDER_URL=https://kc.apps.okddev.fnal.gov/realms/myrealm/.well-known/openid-configuration
OIDC_CLIENT_ID=scd-report-summarizer
OIDC_CLIENT_SECRET_FILE=/run/secrets/oidc_secret

# Optionally block local signups once SSO is working
SCD_DISABLE_LOCAL_SIGNUP=1
```

For the secret file, use a Docker secret or a bind-mounted volume:

```yaml
# compose.yaml (additions)
services:
  web:
    secrets:
      - oidc_secret
    environment:
      OIDC_CLIENT_SECRET_FILE: /run/secrets/oidc_secret

secrets:
  oidc_secret:
    file: ./secrets/oidc_secret.txt
```

---

## Claim mapping

When a user logs in via SSO for the first time, the application maps OIDC claims
from the IdP token to the local User record:

| OIDC claim | Maps to |
|---|---|
| `email` | `user.email` (also used for account lookup) |
| `name` | `user.display_name` |
| `given_name` + `family_name` | `user.display_name` (fallback if `name` absent) |
| `preferred_username` | `user.display_name` (last fallback) |
| `employee_number` / `employeeNumber` / `employee_id` | `user.employee_id` |

On subsequent logins the display name and employee ID are **backfilled only if
they are still blank** — a value already on the account (set at an earlier login
or edited by an administrator) is never overwritten by the IdP. Clear the field
in the Admin Users page if you want the next login to re-take it from the token.

**Role assignment:** roles are not mapped automatically from IdP claims. An
administrator must assign roles (`User`, `Administrator`, `Auditor`) through the
Admin Users page (`/admin-users/`) after a user's first SSO login.

---

## Troubleshooting

### "Log in with SSO" button does not appear

- Confirm `OIDC_PROVIDER_URL`, `OIDC_CLIENT_ID`, and either `OIDC_CLIENT_SECRET_FILE`
  or `OIDC_CLIENT_SECRET` are all set in the environment the server process inherits.
- Check `logs/scd-reporting.log` for import or configuration errors at startup.
- Run `python manage.py check` with the OIDC variables exported — it will surface
  any misconfiguration Django detects.

### `SocialApp matching query does not exist`

This should not occur with the current configuration (the app is defined entirely
in settings, not the database). If it does, confirm that
`allauth.socialaccount.providers.openid_connect` is in `INSTALLED_APPS` and that
`OIDC_ENABLED` is `True` at runtime:

```bash
DJANGO_SETTINGS_MODULE=scd_reporting.settings.prod python -c \
  "import django; django.setup(); from django.conf import settings; print(settings.OIDC_ENABLED)"
```

### `Connection refused` or SSL errors hitting the discovery URL

The application fetches `<server_url>/.well-known/openid-configuration` at login
time. Ensure the server running the application can reach the IdP over the network,
and that any required CA certificates are trusted by the OS.

### Users can log in but land on an error page

Check that the redirect URI registered in the IdP exactly matches:
```
https://<your-hostname>/accounts/oidc/keycloak/login/callback/
```
Trailing slashes and scheme (`http` vs `https`) must match exactly.

---

## Environment variable reference

| Variable | Required | Description |
|---|---|---|
| `OIDC_PROVIDER_URL` | Yes | Discovery URL (`.../.well-known/openid-configuration`) or base realm URL |
| `OIDC_CLIENT_ID` | Yes | Client ID registered in the IdP |
| `OIDC_CLIENT_SECRET_FILE` | One of these | Path to a file whose contents are the client secret |
| `OIDC_CLIENT_SECRET` | One of these | Client secret as a direct env var (file takes precedence) |
| `SCD_DISABLE_LOCAL_SIGNUP` | No | Set to `1` to block new local account creation |
