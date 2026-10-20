---
name: cert-status
description: Check the TLS certificate a host is serving — chain, SAN, expiry, and revocation by OCSP and CRL — and say what a revocation reason means. Use when the user reports a certificate revoked, expired, untrusted or "not secure", asks for the cert status of scd-reporting.fnal.gov or any other host, or wants to confirm a reissue took effect.
---

# Certificate status

For scd-reporting run the project script first; it does every check below:

```bash
./scripts/check-cert.sh                  # served cert + cert-manager state (needs `oc login`)
./scripts/check-cert.sh --no-cluster     # served cert only
./scripts/check-cert.sh -H other.fnal.gov --no-cluster
```

Exit 0 good, 1 problem, 2 usage/tool error, 3 reissue incomplete. Details in `man/check-cert.1`.
If a reissue is needed, use the `cert-reissue` skill.

## Checking by hand (any host)

Put files in the scratchpad. Split the chain so the issuer is available for OCSP;
the awk must parenthesise the filename for BSD awk on macOS.

```bash
H=scd-reporting.fnal.gov
echo | openssl s_client -connect $H:443 -servername $H -showcerts 2>/dev/null \
  | awk '/BEGIN CERT/{n++; f="cert" n ".pem"} f{print > f} /END CERT/{f=""}'
openssl x509 -in cert1.pem -noout -subject -issuer -serial -dates -ext subjectAltName
openssl x509 -in cert1.pem -noout -text | grep -A3 -E "Authority Information|CRL Distribution"

# OCSP (authoritative and current)
openssl ocsp -issuer cert2.pem -cert cert1.pem -url "$(openssl x509 -in cert1.pem -noout -ocsp_uri)" -noverify -resp_text \
  | grep -E "Cert Status|Revocation (Time|Reason)|This Update|Next Update"

# CRL (second source; may lag OCSP by up to its update interval)
curl -s -o crl.der "<CRL URI>"
openssl crl -inform DER -in crl.der -noout -text | grep -A4 "Serial Number: <SERIAL>$"

# Stapling
echo | openssl s_client -connect $H:443 -servername $H -status 2>/dev/null | grep -A2 "OCSP response"
```

If the server sends no intermediate, fetch it from the AIA "CA Issuers" URI
(DER: `openssl x509 -inform DER -in issuer.crt -out cert2.pem`).

Confirm a revocation with both OCSP and CRL before reporting it. Report subject,
issuer, serial, validity, status, revocation time and reason, and when the OCSP
response's Next Update falls.

For scd-reporting, also compare the served serial with the cluster secret. A mismatch
means the router has not reloaded, or the Route is not using the secret:

```bash
oc get secret scd-reporting-tls -n scd-reporting -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -noout -serial -dates
```

## Reading the result

| Revocation reason | Meaning / next step |
|---|---|
| superseded (4) | The CA was told a replacement exists. Usually a second issuance path for the same name (Fermilab cert service, another ACME account) or a CA bulk reissue. Reissue, and get the user to ask the cert admins who requested the replacement, or it may happen again. |
| keyCompromise (1) | Treat the private key as leaked. Reissue with a new key (`rotationPolicy: Always`) and tell the user to investigate. |
| cessationOfOperation (5), affiliationChanged (3) | Usually administrative; reissue. |
| unspecified (0) / none | Ask the CA or the cert admins. |

- **SAN lacks the hostname and the issuer is the cluster wildcard** (`*.apps.okdprod*.fnal.gov`): the router serving this name does not hold the Route. That is a routing problem, not a certificate problem; see the router sharding section of `helm/simple/README.md`.
- **Which clients a revocation breaks:** Firefox (CRLite), and Windows Schannel / .NET / Java when revocation checking is on, reject the cert. Chrome and Safari usually still connect, because they do no online checks for ordinary (non-EV) certs. "It works in my browser" does not mean the cert is good.
- **cert-manager ignores revocation.** A revoked cert keeps `Ready=True` until `status.renewalTime`.

## Certificate Transparency

`https://crt.sh/?q=<host>&output=json&exclude=expired` lists every cert issued for a
name, which is how to find a second issuer behind a "superseded" revocation. crt.sh
is often slow or returns HTML/empty under load: retry, or say the lookup failed.
Don't guess at what it would have shown.
