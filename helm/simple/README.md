# scd-reporting-simple Helm Chart

Deploys the SCD Reporting Django application to an OpenShift/OKD cluster using a
single `web` Deployment backed by two Ceph PVCs (SQLite database + media uploads).

For full deployment instructions see
[Docker-to-OKD-Deployment-Notes.md](../../Docker-to-OKD-Deployment-Notes.md).

---

## Prerequisites

- `helm` CLI ≥ 3.x
- `oc` CLI logged in to the target OKD cluster
- Docker image pushed to `docker.io/normanajn/scd-reporting-web` (see `scripts/build-docker.sh`)

---

## Quick start

```bash
helm upgrade --install scd-reporting ./helm/simple \
  -n scd-reporting \
  -f /path/to/my-values.yaml
```

Force a pod restart to pull the latest image:

```bash
oc rollout restart deployment/web -n scd-reporting
# or
./scripts/restart-pod.sh
```

---

## Values reference

| Key | Default | Description |
|---|---|---|
| `image.repository` | `docker.io/normanajn/scd-reporting-web` | Docker image |
| `image.tag` | `latest` | Image tag |
| `image.pullPolicy` | `Always` | Pull policy — `Always` required for `:latest` |
| `replicaCount` | `1` | Number of web pods |
| `service.port` | `8000` | ClusterIP service port |
| `route.hostname` | `""` | Public hostname — creates an OKD Route when set |
| `route.timeout` | `300s` | HAProxy router request timeout — keep >= `gunicorn.timeout` |
| `route.ingressController` | `""` | Value of the `ingresscontroller` Route label routers shard on. **Leave empty** unless DNS is repointed at the same router — see warning below |
| `certManager.enabled` | `false` | Create a cert-manager `Certificate` resource |
| `certManager.clusterIssuer` | `incommon-acme` | `ClusterIssuer` name |
| `certManager.secretName` | `scd-reporting-tls` | Secret cert-manager writes the TLS cert into |
| `certManager.externalCertificate` | `false` | Wire TLS secret into Route + grant router RBAC |
| `pvc.sqliteData.size` | `1Gi` | PVC size for SQLite database (`/app/data`) |
| `pvc.media.size` | `1Gi` | PVC size for user uploads (`/app/media`) |
| `django.settingsModule` | `scd_reporting.settings.dev` | Django settings module |
| `django.secretKey` | *(placeholder)* | Django `SECRET_KEY` — **change before deploying** |
| `django.allowedHosts` | `localhost,127.0.0.1` | Additional `ALLOWED_HOSTS` entries |
| `django.emailVerification` | `optional` | allauth email verification: `none`, `optional`, `mandatory` |
| `django.disableLocalSignup` | `"0"` | Set to `"1"` to block new local account creation |
| `django.initialAdminUsername` | `scd-admin` | Bootstrap admin username |
| `django.initialAdminEmail` | `scd-admin@fnal.gov` | Bootstrap admin email |
| `django.initialAdminPassword` | *(placeholder)* | Bootstrap admin password — **change before deploying** |
| `database.url` | `sqlite:////app/data/db.sqlite3` | `DATABASE_URL` connection string |
| `gunicorn.workers` | `3` | Gunicorn worker count |
| `gunicorn.logLevel` | `info` | Gunicorn log level |
| `anthropic.apiKey` | `""` | Anthropic (or LiteLLM) API key |
| `anthropic.summaryModel` | `claude-sonnet-4-6` | Model for AI report summaries |
| `anthropic.baseUrl` | `""` | Custom API base URL (e.g. a LiteLLM proxy); leave empty for standard Anthropic API |
| `email.host` | `""` | SMTP host |
| `email.port` | `"587"` | SMTP port |
| `email.user` | `""` | SMTP username |
| `email.password` | `""` | SMTP password |
| `email.defaultFrom` | `noreply@localhost` | Default `From:` address |
| `google.clientId` | `""` | Google OAuth client ID |
| `google.clientSecret` | `""` | Google OAuth client secret |
| `oidc.providerUrl` | `""` | OIDC discovery URL (enables SSO when set with clientId + clientSecret) |
| `oidc.clientId` | `""` | OIDC client ID |
| `oidc.clientSecret` | `""` | OIDC client secret |

---

## Secrets

Sensitive values (`django.secretKey`, `django.initialAdminPassword`, `oidc.clientSecret`,
`anthropic.apiKey`, `email.password`, `google.clientSecret`) are written into a
Kubernetes `Secret` object and injected into the pod via `envFrom`. **Never commit
these values to git** — pass them via a local `my-values.yaml` override file:

```yaml
# my-values.yaml  (outside the repo or git-ignored)
django:
  secretKey: "..."
  initialAdminPassword: "..."
oidc:
  clientSecret: "..."
anthropic:
  apiKey: "..."
  summaryModel: "azure/claude-sonnet-4-6"
  baseUrl: "https://litellm.fnal.gov"
route:
  hostname: scd-reporting.fnal.gov
certManager:
  enabled: true
  externalCertificate: true
```

---

## Router sharding (`route.ingressController`) — read before setting

The cluster runs two IngressControllers, each with its own ingress endpoint:

| Router | Default cert | IP |
|---|---|---|
| `default` | `*.apps.okdprod1.fnal.gov` | `131.225.163.40` |
| `public-proxy` | `*.apps.okdprod.fnal.gov` | `131.225.163.71` |

The `ingresscontroller` Route label decides which router serves the route. DNS decides
where clients go. **These two must agree** — if they disagree the hostname lands on a
router that no longer holds the route, which answers with its own wildcard cert and
`503 Application is not available`. Changing one without the other is a hard outage
(this happened on 2026-08-06).

Current state, as of 2026-08-06:

```
scd-reporting.fnal.gov -> okdprod1-ingress.fnal.gov -> 131.225.163.71   (public-proxy)
route.ingressController: public-proxy
```

Either direction of change is **two parts that must land together**:

1. Networking repoints `scd-reporting.fnal.gov` at the target router's IP.
2. This value is set to match (`public-proxy`, or `""` for the default router).

Note the TTL: DNS records carry a 21600s (6h) TTL, so after a repoint, clients with a
cached answer keep hitting the *old* router until their cache expires. Expect a tail of
503s during that window — it is not a misconfiguration.

Verify after any change to this value:

```bash
oc get route web -n scd-reporting -o jsonpath='{range .status.ingress[*]}{.routerName}{"\t"}{.routerCanonicalHostname}{"\n"}{end}'
curl -sS -o /dev/null -w '%{http_code}\n' https://scd-reporting.fnal.gov/   # expect 302
```

---

## TLS / cert-manager

Setting `certManager.enabled=true` creates a `cert-manager.io/v1 Certificate` resource.
Because the OKD Route cannot reference a secret that does not yet exist, the first-time
setup is a two-step process:

**Step 1** — issue the certificate:

```bash
helm upgrade scd-reporting ./helm/simple -n scd-reporting \
  -f my-values.yaml --set certManager.enabled=true --set certManager.externalCertificate=false
oc get certificate -n scd-reporting -w   # wait for READY = True
```

**Step 2** — wire into the Route:

```bash
helm upgrade scd-reporting ./helm/simple -n scd-reporting \
  -f my-values.yaml --set certManager.enabled=true --set certManager.externalCertificate=true
```

After this, add both flags to `my-values.yaml` for all future upgrades.

---

## Rollback

```bash
helm history scd-reporting -n scd-reporting
helm rollback scd-reporting -n scd-reporting        # one revision back
helm rollback scd-reporting 5 -n scd-reporting      # to a specific revision
```
