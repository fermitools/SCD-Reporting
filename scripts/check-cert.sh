#!/usr/bin/env bash
# Check the TLS certificate served by SCD Reporting (revocation, expiry, hostname,
# match with the cert-manager secret) and optionally reissue it through cert-manager.
#
# Usage:
#   ./scripts/check-cert.sh [OPTIONS]
#
# Options:
#   -H HOSTNAME      Hostname to check (default: $SCD_HOSTNAME, or scd-reporting.fnal.gov)
#   -n NAMESPACE     OKD namespace (default: $SCD_NAMESPACE, or scd-reporting)
#   -c CERTIFICATE   cert-manager Certificate name (default: $SCD_CERT_NAME, or scd-reporting)
#   -d DAYS          Warn when the certificate expires within DAYS days (default: 21)
#   --reissue        Reissue through cert-manager if a problem is found
#   --force          Reissue even if no problem is found (implies --reissue)
#   --recreate       Reissue by deleting and recreating the Certificate object
#                    (implies --reissue). Clears cert-manager's failure backoff;
#                    the TLS secret and the Route are left in place.
#   -b DIR           Where --recreate saves a JSON backup of the Certificate
#                    (default: $SCD_CERT_BACKUP_DIR, or $TMPDIR)
#   -w SECONDS       Seconds to wait for a reissue to finish (default: 600)
#   --no-cluster     Skip the oc checks; only inspect the served certificate
#   --dry-run        Print the reissue commands without executing them
#   -h               Show this help message
#
# Exit status:
#   0  certificate is good (or was reissued and the new one is good)
#   1  problem found (revoked, expired, expiring, wrong name, secret mismatch)
#   2  usage error or missing tool
#   3  reissue was requested but did not complete
#
# Environment variables:
#   SCD_HOSTNAME, SCD_NAMESPACE, SCD_CERT_NAME   Defaults for -H, -n, -c
#   SCD_CERT_BACKUP_DIR                          Default for -b

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
HOSTNAME_="${SCD_HOSTNAME:-scd-reporting.fnal.gov}"
NAMESPACE="${SCD_NAMESPACE:-scd-reporting}"
CERT_NAME="${SCD_CERT_NAME:-scd-reporting}"
WARN_DAYS=21
WAIT=600
REISSUE=false
FORCE=false
RECREATE=false
BACKUP_DIR="${SCD_CERT_BACKUP_DIR:-${TMPDIR:-/tmp}}"
CLUSTER=true
DRY_RUN=false

# ── Helpers ───────────────────────────────────────────────────────────────────
step() { echo; echo "── $* ──"; }
info() { echo "   $*"; }
ok()   { echo "   ✓ $*"; }
bad()  { echo "   ✗ $*"; PROBLEMS+=("$*"); }
die()  { echo; echo "ERROR: $*" >&2; exit 2; }

run() {
    if [[ "${DRY_RUN}" == true ]]; then
        echo "   [dry-run] $*"
    else
        "$@"
    fi
}

usage() {
    sed -n '/^# Usage:/,/^[^#]/{ /^#/{ s/^# \{0,2\}//; p } }' "$0"
    exit 0
}

# ISO-8601 UTC timestamp -> epoch seconds (GNU date, then BSD date).
epoch() {
    date -u -d "$1" +%s 2>/dev/null || date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$1" +%s
}

WORK="$(mktemp -d "${TMPDIR:-/tmp}/check-cert.XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT INT TERM

# ── Argument parsing ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        -H)           HOSTNAME_="$2";  shift 2 ;;
        -n)           NAMESPACE="$2";  shift 2 ;;
        -c)           CERT_NAME="$2";  shift 2 ;;
        -d)           WARN_DAYS="$2";  shift 2 ;;
        -w)           WAIT="$2";       shift 2 ;;
        --reissue)    REISSUE=true;    shift ;;
        --force)      REISSUE=true; FORCE=true; shift ;;
        --recreate)   REISSUE=true; RECREATE=true; shift ;;
        -b)           BACKUP_DIR="$2"; shift 2 ;;
        --no-cluster) CLUSTER=false;   shift ;;
        --dry-run)    DRY_RUN=true;    shift ;;
        -h|--help)    usage ;;
        *) die "Unknown option: $1" ;;
    esac
done

for tool in openssl curl; do
    command -v "${tool}" >/dev/null || die "${tool} not found in PATH"
done
if [[ "${CLUSTER}" == true ]]; then
    command -v oc >/dev/null || die "oc not found in PATH (use --no-cluster to skip cluster checks)"
    oc whoami >/dev/null 2>&1 || die "not logged in to OKD — run 'oc login' first (or use --no-cluster)"
fi
[[ "${REISSUE}" == true && "${CLUSTER}" == false ]] && die "--reissue needs cluster access; drop --no-cluster"
if [[ "${RECREATE}" == true ]]; then
    command -v python3 >/dev/null || die "--recreate needs python3 to rewrite the Certificate manifest"
    [[ -d "${BACKUP_DIR}" && -w "${BACKUP_DIR}" ]] || die "backup directory ${BACKUP_DIR} is not writable (use -b DIR)"
fi

# Fetch the chain the router is serving into ${WORK}/cert1.pem (leaf), cert2.pem (issuer), ...
fetch_served() {
    rm -f "${WORK}"/cert*.pem
    echo | openssl s_client -connect "${HOSTNAME_}:443" -servername "${HOSTNAME_}" -showcerts 2>/dev/null \
        | awk -v dir="${WORK}" '/BEGIN CERTIFICATE/{n++; f=dir "/cert" n ".pem"} f{print > f} /END CERTIFICATE/{f=""}'
    [[ -s "${WORK}/cert1.pem" ]]
}

serial_of() { openssl x509 -in "$1" -noout -serial | cut -d= -f2; }

# Revocation status of leaf $1 issued by $2: prints "good", "revoked <time> <reason>", or "unknown".
revocation_status() {
    local leaf="$1" issuer="$2" ocsp_url crl_url serial out
    ocsp_url="$(openssl x509 -in "${leaf}" -noout -ocsp_uri 2>/dev/null | head -1)"
    if [[ -z "${ocsp_url}" ]]; then
        ocsp_url="$(openssl x509 -in "${leaf}" -noout -text | awk '/OCSP - URI:/{sub(/.*URI:/,""); print; exit}')"
    fi
    if [[ -n "${ocsp_url}" && -s "${issuer}" ]]; then
        out="$(openssl ocsp -issuer "${issuer}" -cert "${leaf}" -url "${ocsp_url}" -noverify -resp_text 2>/dev/null || true)"
        if grep -q 'Cert Status: good' <<<"${out}"; then echo "good (OCSP ${ocsp_url})"; return; fi
        if grep -q 'Cert Status: revoked' <<<"${out}"; then
            echo "revoked $(awk -F': ' '/Revocation Time/{print $2; exit}' <<<"${out}") reason=$(awk -F': ' '/Revocation Reason/{print $2; exit}' <<<"${out}") (OCSP)"
            return
        fi
    fi
    # Fall back to the CRL.
    crl_url="$(openssl x509 -in "${leaf}" -noout -text | awk '/CRL Distribution/{f=1} f&&/URI:/{sub(/.*URI:/,""); print; exit}')"
    if [[ -n "${crl_url}" ]] && curl -sf --max-time 30 -o "${WORK}/crl.der" "${crl_url}"; then
        serial="$(serial_of "${leaf}")"
        out="$(openssl crl -inform DER -in "${WORK}/crl.der" -noout -text 2>/dev/null | grep -A4 "Serial Number: ${serial}\$" || true)"
        if [[ -z "${out}" ]]; then echo "good (CRL ${crl_url})"; return; fi
        echo "revoked $(awk -F': ' '/Revocation Date/{print $2; exit}' <<<"${out}") reason=$(tail -1 <<<"${out}" | tr -d ' ') (CRL)"
        return
    fi
    echo "unknown (no OCSP answer and CRL unavailable)"
}

# Run every check on the served certificate. Fills PROBLEMS.
check_served() {
    PROBLEMS=()
    step "Served certificate: ${HOSTNAME_}:443"
    fetch_served || { bad "could not retrieve a certificate from ${HOSTNAME_}:443"; return; }
    local leaf="${WORK}/cert1.pem" issuer="${WORK}/cert2.pem" rev
    SERVED_SERIAL="$(serial_of "${leaf}")"
    info "Subject:  $(openssl x509 -in "${leaf}" -noout -subject | sed 's/^subject=//')"
    info "Issuer:   $(openssl x509 -in "${leaf}" -noout -issuer  | sed 's/^issuer=//')"
    info "Serial:   ${SERVED_SERIAL}"
    info "Valid:    $(openssl x509 -in "${leaf}" -noout -startdate | cut -d= -f2) → $(openssl x509 -in "${leaf}" -noout -enddate | cut -d= -f2)"

    if openssl x509 -in "${leaf}" -noout -ext subjectAltName 2>/dev/null | grep -qE "DNS:${HOSTNAME_}(,|$)"; then
        ok "SAN contains ${HOSTNAME_}"
    else
        bad "SAN does not contain ${HOSTNAME_} (router may be serving its wildcard default — see helm/simple/README.md, Router sharding)"
    fi

    if ! openssl x509 -in "${leaf}" -noout -checkend 0 >/dev/null; then
        bad "certificate has expired"
    elif ! openssl x509 -in "${leaf}" -noout -checkend $((WARN_DAYS * 86400)) >/dev/null; then
        bad "certificate expires within ${WARN_DAYS} days"
    else
        ok "not expiring within ${WARN_DAYS} days"
    fi

    [[ -s "${issuer}" ]] || info "server sent no intermediate; OCSP check will fall back to CRL"
    rev="$(revocation_status "${leaf}" "${issuer}")"
    case "${rev}" in
        good*)    ok "revocation: ${rev}" ;;
        revoked*) bad "revocation: ${rev}" ;;
        *)        info "revocation: ${rev}" ;;
    esac
}

# Compare the cluster's Certificate/secret with what is served. Fills PROBLEMS.
check_cluster() {
    step "cert-manager: ${NAMESPACE}/${CERT_NAME}"
    if ! oc get certificate "${CERT_NAME}" -n "${NAMESPACE}" >/dev/null 2>&1; then
        bad "Certificate ${CERT_NAME} not found in ${NAMESPACE}"
        return
    fi
    local jp='{.spec.secretName}|{.status.conditions[?(@.type=="Ready")].status}|{.status.conditions[?(@.type=="Ready")].message}|{.status.renewalTime}|{.status.failedIssuanceAttempts}|{.status.lastFailureTime}|{.spec.privateKey.rotationPolicy}'
    IFS='|' read -r SECRET_NAME READY READY_MSG RENEWAL FAILED LAST_FAIL ROTATION \
        <<<"$(oc get certificate "${CERT_NAME}" -n "${NAMESPACE}" -o jsonpath="${jp}")"
    if [[ "${READY}" == True ]]; then
        ok "Certificate Ready"
    else
        bad "Certificate not Ready: ${READY_MSG:-no Ready condition}"
    fi
    info "Renewal time:   ${RENEWAL:-?}"
    info "Key rotation:   ${ROTATION:-Never (default)}"
    if [[ -n "${FAILED}" && "${FAILED}" != 0 ]]; then
        info "Failed issues:  ${FAILED} (last ${LAST_FAIL})"
    fi
    # Requests are named <cert>-<revision>; an in-flight or failed one is <cert>-<revision+1>.
    local rev cr
    rev="$(oc get certificate "${CERT_NAME}" -n "${NAMESPACE}" -o jsonpath='{.status.revision}')"
    for cr in "${CERT_NAME}-$(( ${rev:-0} + 1 ))" "${CERT_NAME}-${rev:-0}"; do
        if oc get certificaterequest "${cr}" -n "${NAMESPACE}" >/dev/null 2>&1; then
            info "Last request:   ${cr} $(oc get certificaterequest "${cr}" -n "${NAMESPACE}" \
                -o jsonpath='{.status.conditions[?(@.type=="Ready")].reason}: {.status.conditions[?(@.type=="Ready")].message}' | cut -c1-160)"
            break
        fi
    done

    SECRET_SERIAL="$(oc get secret "${SECRET_NAME}" -n "${NAMESPACE}" -o jsonpath='{.data.tls\.crt}' 2>/dev/null \
        | openssl base64 -d -A | openssl x509 -noout -serial 2>/dev/null | cut -d= -f2 || true)"
    if [[ -z "${SECRET_SERIAL}" ]]; then
        bad "secret ${SECRET_NAME} missing or has no tls.crt"
    elif [[ "${SECRET_SERIAL}" == "${SERVED_SERIAL:-}" ]]; then
        ok "secret ${SECRET_NAME} matches served cert"
    else
        bad "secret ${SECRET_NAME} serial ${SECRET_SERIAL} differs from served ${SERVED_SERIAL:-?} (router has not reloaded, or Route is not using the secret)"
    fi
}

# Force cert-manager to reissue. Changing the secret's issuer-name annotation trips
# cert-manager's SecretIssuerAnnotationsMismatch trigger; the old cert stays in the
# secret (and on the Route) until the new one is written, so there is no gap. This
# is used instead of 'cmctl renew' because that needs write access to
# certificates/status, which namespace users do not have on okdprod.
#
# With --recreate the Certificate is deleted and recreated instead. cert-manager keeps
# its failure backoff only in the Certificate's status, so a new object has none.
# Because the secret's issuer annotation is changed first, the new Certificate
# reissues rather than adopting the cert already in the secret.
reissue() {
    step "Reissue"
    local now retry backoff rev_before cr deadline state
    if [[ "${RECREATE}" == true ]]; then
        recreate_certificate || return 1
        [[ "${DRY_RUN}" == true ]] && return 0
        rev_before=""
        FAILED=""
        wait_for_issuance "${CERT_NAME}-1" "${rev_before}"
        return
    fi

    if [[ "${ROTATION:-}" != "Always" ]]; then
        info "Setting privateKey.rotationPolicy=Always (a reused key resubmits the old CSR, which InCommon ACME rejects as badCSR)"
        run oc patch certificate "${CERT_NAME}" -n "${NAMESPACE}" --type merge \
            -p '{"spec":{"privateKey":{"rotationPolicy":"Always"}}}'
    fi

    # cert-manager holds off 2^(n-1) h (max 32 h) after the n-th failed issuance.
    # The status subresource that cmctl clears is not writable from here, so report it.
    if [[ -n "${FAILED}" && "${FAILED}" != 0 && -n "${LAST_FAIL}" ]]; then
        backoff=$(( 3600 * (1 << (FAILED - 1)) )); (( backoff > 115200 )) && backoff=115200
        now="$(date -u +%s)"; retry=$(( $(epoch "${LAST_FAIL}") + backoff ))
        if (( retry > now )); then
            info "cert-manager is in failure backoff after ${FAILED} failed attempt(s);"
            info "it will retry automatically at $(date -u -r "${retry}" +%FT%TZ 2>/dev/null || date -u -d "@${retry}" +%FT%TZ)."
            info "Re-run this script after that time to confirm, or use --recreate to skip the wait."
            return 1
        fi
    fi

    rev_before="$(oc get certificate "${CERT_NAME}" -n "${NAMESPACE}" -o jsonpath='{.status.revision}')"
    run oc annotate secret "${SECRET_NAME}" -n "${NAMESPACE}" --overwrite cert-manager.io/issuer-name=reissue-requested
    [[ "${DRY_RUN}" == true ]] && return 0

    wait_for_issuance "${CERT_NAME}-$(( ${rev_before:-0} + 1 ))" "${rev_before}"
}

# Delete the Certificate and create it again from its own live definition, keeping
# the Helm labels and annotations so the next 'helm upgrade' still owns it.
recreate_certificate() {
    local owners backup manifest="${WORK}/certificate.json"
    owners="$(oc get secret "${SECRET_NAME}" -n "${NAMESPACE}" \
        -o jsonpath='{.metadata.ownerReferences[?(@.kind=="Certificate")].name}' 2>/dev/null || true)"
    if [[ -n "${owners}" ]]; then
        bad "secret ${SECRET_NAME} is owned by Certificate ${owners} (cert-manager runs with --enable-certificate-owner-ref);"
        bad "deleting the Certificate would delete the secret and the Route would lose its cert. Use --reissue instead."
        return 1
    fi

    backup="${BACKUP_DIR%/}/${CERT_NAME}-certificate-$(date -u +%Y%m%dT%H%M%SZ).json"
    oc get certificate "${CERT_NAME}" -n "${NAMESPACE}" -o json > "${backup}"
    info "Backup of the live Certificate: ${backup}"

    # Strip server-set fields and status (where the backoff lives); force a fresh key.
    python3 - "${backup}" > "${manifest}" <<'EOF'
import json, sys
obj = json.load(open(sys.argv[1]))
obj.pop("status", None)
for k in ("uid", "resourceVersion", "creationTimestamp", "generation", "managedFields", "selfLink"):
    obj["metadata"].pop(k, None)
obj["spec"].setdefault("privateKey", {})["rotationPolicy"] = "Always"
json.dump(obj, sys.stdout, indent=2)
EOF
    # Validate before deleting anything.
    oc create --dry-run=client -f "${manifest}" -o name >/dev/null \
        || { bad "rewritten Certificate manifest does not validate; nothing was changed"; return 1; }

    run oc annotate secret "${SECRET_NAME}" -n "${NAMESPACE}" --overwrite cert-manager.io/issuer-name=reissue-requested
    run oc delete certificate "${CERT_NAME}" -n "${NAMESPACE}" --wait=true
    if ! run oc create -f "${manifest}"; then
        bad "recreating Certificate ${CERT_NAME} failed; the secret and Route are untouched. Restore with:"
        bad "  python3 -c 'import json,sys; o=json.load(open(sys.argv[1])); o.pop(\"status\",None); [o[\"metadata\"].pop(k,None) for k in (\"uid\",\"resourceVersion\",\"creationTimestamp\",\"generation\",\"managedFields\")]; print(json.dumps(o))' ${backup} | oc create -f -"
        return 1
    fi
}

# Wait for cert-manager to finish issuing, then for the router to serve the result.
# Progress is read from the Certificate, not the CertificateRequest: a retry reuses
# the name <cert>-<revision+1>, so a stale failed request can carry the same name.
# Success bumps status.revision; failure bumps status.failedIssuanceAttempts.
wait_for_issuance() {
    local cr="$1" rev_before="$2" deadline state=""
    info "Waiting up to ${WAIT}s for CertificateRequest ${cr}"
    deadline=$(( $(date +%s) + WAIT ))
    while (( $(date +%s) < deadline )); do
        state="$(oc get certificate "${CERT_NAME}" -n "${NAMESPACE}" \
            -o jsonpath='{.status.revision}/{.status.failedIssuanceAttempts}' 2>/dev/null || true)"
        # An empty answer is an oc error, not progress.
        [[ -z "${state}" ]] && { sleep 10; continue; }
        if [[ "${state%%/*}" != "${rev_before}" ]]; then
            ok "${cr} issued (revision ${state%%/*})"; break
        fi
        if [[ "${state#*/}" != "${FAILED}" && -n "${state#*/}" ]]; then
            bad "${cr} failed: $(oc get certificaterequest "${cr}" -n "${NAMESPACE}" \
                -o jsonpath='{.status.conditions[?(@.type=="Ready")].message}' 2>/dev/null)"
            return 1
        fi
        sleep 10
    done
    [[ -n "${state}" && "${state%%/*}" != "${rev_before}" ]] || { bad "${cr} not issued after ${WAIT}s"; return 1; }

    # The router picks up the new secret on its own; give it time.
    info "Waiting for the router to serve the new certificate"
    for _ in $(seq 1 18); do
        fetch_served && [[ "$(serial_of "${WORK}/cert1.pem")" != "${SERVED_SERIAL}" ]] && return 0
        sleep 10
    done
    bad "router still serves ${SERVED_SERIAL} after 3 minutes"
    return 1
}

# ── Main ──────────────────────────────────────────────────────────────────────
PROBLEMS=()
SERVED_SERIAL=""
check_served
[[ "${CLUSTER}" == true ]] && check_cluster

if (( ${#PROBLEMS[@]} == 0 )) && [[ "${FORCE}" != true ]]; then
    step "Result"; ok "certificate is good"; exit 0
fi

if [[ "${REISSUE}" != true ]]; then
    step "Result"
    for p in "${PROBLEMS[@]}"; do info "✗ ${p}"; done
    info "Re-run with --reissue to request a new certificate."
    exit 1
fi

reissue || { step "Result"; info "Reissue did not complete."; exit 3; }
[[ "${DRY_RUN}" == true ]] && exit 0

check_served
check_cluster
step "Result"
if (( ${#PROBLEMS[@]} == 0 )); then ok "new certificate is good"; exit 0; fi
for p in "${PROBLEMS[@]}"; do info "✗ ${p}"; done
exit 1
