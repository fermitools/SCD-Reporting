---
name: cert-reissue
description: Reissue the scd-reporting TLS certificate through cert-manager on okdprod without an outage — the annotation trigger, delete-and-recreate to get around the failure backoff, private-key rotation, and verifying the result. Use when a certificate is revoked, expiring, mismatched or stuck not-Ready and needs a new one, or when the user asks to renew, reissue or rotate the cert.
---

# Reissuing the certificate (cert-manager on OKD)

Setup: Certificate `scd-reporting` in namespace `scd-reporting`, ClusterIssuer
`incommon-acme` (InCommon/emSign ACME), secret `scd-reporting-tls`, consumed by Route
`web` via `spec.tls.externalCertificate`. Everything is Helm-managed by release
`scd-reporting`.

Reissuing requests a production certificate from an external CA. Confirm with the
user first unless they have already said to go ahead.

## Use the script

```bash
./scripts/check-cert.sh --reissue            # annotation trigger; refuses during backoff
./scripts/check-cert.sh --recreate -b logs   # delete + recreate; skips the backoff
./scripts/check-cert.sh --recreate --force --dry-run   # preview, changes nothing
```

The script picks no method on its own: use `--reissue` first, and `--recreate` when it
reports backoff or the user wants it done now. Afterwards it re-runs every check, and
exit 0 means the router is serving a new cert that passes OCSP.

## Methods, and why

1. **`cmctl renew` or patching `.status` (the Issuing condition)** — the canonical way, but it does not work here. Namespace users get `Forbidden` on `certificates/status`, even though `oc auth can-i patch certificates/status` answers yes. cmctl is also not installed.
2. **Secret issuer-annotation trigger** (`--reissue`):
   `oc annotate secret scd-reporting-tls -n scd-reporting --overwrite cert-manager.io/issuer-name=reissue-requested`
   This trips cert-manager's `SecretIssuerAnnotationsMismatch` trigger. The old cert stays in the secret until the new one is written, so there is no outage. cert-manager rewrites the annotation itself on success. Meanwhile the Certificate shows `Ready=False`, reason `IncorrectIssuer`, which is expected.
3. **Delete and recreate the Certificate** (`--recreate`) — clears the failure backoff, because cert-manager keeps it only in `.status`. Safe only if the secret has no `ownerReferences` to the Certificate. Check with `oc get secret scd-reporting-tls -o jsonpath='{.metadata.ownerReferences}'`, which is currently empty: cert-manager runs without `--enable-certificate-owner-ref`. Recreate from the live object (`oc get -o json`, strip status/uid/resourceVersion/creationTimestamp/generation/managedFields) so the Helm labels and `meta.helm.sh/*` annotations survive. Change the secret's issuer annotation first; otherwise the new Certificate finds a matching secret and adopts the old cert. Request numbering restarts at `scd-reporting-1`.

**Never delete the secret to force a reissue.** The Route references the secret by
name, so until cert-manager writes a new one the router serves its wildcard default
cert, which is an outage.

## Pitfalls seen in production (2026-10-20)

- **`badCSR: You are attempting to submit a CSR of an existing certificate order`** — cert-manager reused the stored key (`privateKey.rotationPolicy: Never`, the default before v1.18), so the CSR was byte-identical to the previous order's. Fix: `rotationPolicy: Always`, which is now set in the chart (`certManager.privateKeyRotationPolicy`) and enforced by the script. The event `Reused private key` before a request means the CSR will repeat; `Generated` means a new key.
- **Failure backoff** — after the n-th failed issuance, cert-manager waits 2^(n-1) h (max 32 h) before retrying; see `status.failedIssuanceAttempts` and `status.lastFailureTime`. Changing the spec or annotations does not clear it. Either wait, or use `--recreate`.
- **Retries reuse the request name.** A retry is named `<cert>-<status.revision+1>`, the same as the failed one. Track progress with `status.revision` (goes up on success) and `status.failedIssuanceAttempts` (goes up on failure), not with a CertificateRequest name.

## Verify

```bash
oc get certificate,certificaterequest,order -n scd-reporting
oc get events -n scd-reporting --field-selector involvedObject.kind=Certificate --sort-by=.lastTimestamp | tail
./scripts/check-cert.sh          # served serial changed, OCSP good, secret == served
curl -sS -o /dev/null -w '%{http_code}\n' https://scd-reporting.fnal.gov/   # 302
```

The router usually serves the new secret within about 10 s of issuance. If it still
serves the old serial after a few minutes, check the `router-secret-rolebinding`
template (the router needs RBAC to read the secret) and the Route's `externalCertificate`.

If issuance fails, use the `cert-troubleshoot` skill.
