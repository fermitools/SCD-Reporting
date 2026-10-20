---
name: cert-troubleshoot
description: Diagnose cert-manager and TLS failures for scd-reporting on okdprod — walk the Certificate → CertificateRequest → Order → Challenge chain, read the ACME error, and tell certificate problems apart from router or DNS problems. Use when a reissue fails, the Certificate is stuck not-Ready, the site serves the wrong or wildcard cert, or a cert was revoked and the cause is unknown.
---

# Troubleshooting certificates

## Walk the chain, top to bottom

```bash
NS=scd-reporting
oc get certificate scd-reporting -n $NS -o jsonpath='{.status}' | python3 -m json.tool
oc get certificaterequest,order,challenge -n $NS
oc get certificaterequest <cr> -n $NS -o jsonpath='{range .status.conditions[*]}{.type}={.status} {.reason}: {.message}{"\n"}{end}'
oc get order -n $NS -o jsonpath='{range .items[*]}{.metadata.name} {.status.state} {.status.reason}{"\n"}{end}'
oc get events -n $NS --sort-by=.lastTimestamp | grep -iE "cert|order|challenge|issu" | tail -20
```

The useful fields on the Certificate: the `Ready` and `Issuing` conditions,
`revision`, `failedIssuanceAttempts`, `lastFailureTime`, `renewalTime`. Namespace users
cannot list cluster-scoped deployments, so you cannot read the cert-manager
controller logs or its version. Work from events and the status fields.

## Known errors

| Symptom | Cause | Fix |
|---|---|---|
| Order `errored`: `badCSR ... CSR of an existing certificate order` | The CSR repeats a previous order because the key was reused (`rotationPolicy: Never`) | `rotationPolicy: Always`, then reissue (`cert-reissue` skill) |
| No new CertificateRequest after a trigger, `failedIssuanceAttempts > 0` | cert-manager failure backoff, 2^(n-1) h | Wait until `lastFailureTime` + backoff, or `./scripts/check-cert.sh --recreate` |
| `Ready=False`, reason `IncorrectIssuer`, mentions `reissue-requested` | The annotation trigger fired and issuance is in progress or failed | Look at the newest CertificateRequest; normal while a reissue is in flight |
| `Forbidden ... certificates/status` | Namespace RBAC; `can-i` says yes but the server denies it | Don't use cmctl or status patches; use the annotation or recreate methods |
| Certificate `Ready=True` but clients report revoked | cert-manager does not check revocation | Confirm with OCSP (`cert-status` skill), then reissue |
| Site serves `*.apps.okdprod*.fnal.gov` with a 503 | The hostname's DNS points at a router that doesn't hold the Route (sharding label vs DNS) | See "Router sharding" in `helm/simple/README.md`. Not a cert problem; don't reissue |
| Secret updated but the router serves the old serial | The router can't read the secret, or the Route doesn't reference it | Check that the `router-secret-rolebinding` exists and the Route has `spec.tls.externalCertificate.name` |
| Order stuck `pending`, Challenge failing | The ACME http-01 challenge can't reach the solver (DNS/router) | `oc describe challenge`; check the hostname resolves to the router that holds the solver route |

## Finding the cause of a revocation

The revocation reason comes from OCSP or the CRL (`cert-status` skill). For
*superseded*, look in CT logs (crt.sh) for a cert for the same name issued around
the revocation time by something other than this cluster. Compare its serial and
issue time with the CertificateRequests here. Anything not from this cluster points
to a second issuance path, which the Fermilab cert admins have to resolve. Report
what was found; if the CT lookup failed, say so rather than inferring.

## Reporting

State which object failed, the exact error text, the cause, and what was or wasn't
changed on the cluster. Before you claim a fix, check the served serial and the OCSP
status, or run `./scripts/check-cert.sh`.
