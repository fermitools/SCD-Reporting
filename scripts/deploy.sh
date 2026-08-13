#!/usr/bin/env bash
# Build, push, and deploy SCD Reporting to the OKD cluster.
#
# Usage:
#   ./scripts/deploy.sh [OPTIONS]
#
# Options:
#   -f FILE          Path to Helm values file with secrets
#                    (default: $SCD_VALUES_FILE, or ~/scd-reporting-values.yaml)
#   -t TAG           Docker image tag / git release tag (default: latest)
#   -n NAMESPACE     OKD namespace (default: scd-reporting)
#   --skip-push      Skip git push
#   --skip-build     Skip Docker build and push (Helm + restart only)
#   --skip-helm      Skip Helm upgrade (build + restart only)
#   --no-cache       Pass --no-cache to docker buildx build
#   --vault          Fetch a Vault AppRole token with
#                    scripts/get-vault-apptoken.sh and pass it to Helm, so the
#                    pod can retrieve its own secrets from Vault
#   --vault-role R   AppRole role name to use with --vault
#   --vault-path P   KV v2 prefix the app reads its secrets from, mount
#                    included (default: okd/shared/prod/scd-reporting).
#                    Use okd/shared/test/scd-reporting for the test instance.
#   --dry-run        Print commands without executing them
#   -h               Show this help message
#
# Environment variables:
#   SCD_VALUES_FILE  Default path to the Helm values file
#   SCD_VAULT_ROLE   Default AppRole role name for --vault
#   SCD_VAULT_PATH   Default secret path for --vault

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ── Defaults ──────────────────────────────────────────────────────────────────
NAMESPACE="scd-reporting"
RELEASE="scd-reporting"
CHART="${REPO_ROOT}/helm/simple"
TAG=""
SKIP_PUSH=false
SKIP_BUILD=false
SKIP_HELM=false
NO_CACHE=false
DRY_RUN=false
USE_VAULT=false
VAULT_ROLE="${SCD_VAULT_ROLE:-}"
VAULT_PATH="${SCD_VAULT_PATH:-okd/shared/prod/scd-reporting}"
VAULT_VALUES_FILE=""

# Locate the values file: flag > env var > well-known paths
VALUES_FILE="${SCD_VALUES_FILE:-}"
CANDIDATE_PATHS=(
    "${HOME}/scd-reporting-values.yaml"
    "${HOME}/Credentials/scd-reporting/values.yaml"
    "${REPO_ROOT}/../scd-reporting-values.yaml"
)

# ── Helpers ───────────────────────────────────────────────────────────────────
step() { echo; echo "── $* ──────────────────────────────────────────────────────" | head -c 64; echo; }
info() { echo "   $*"; }
ok()   { echo "   ✓ $*"; }
die()  { echo; echo "ERROR: $*" >&2; exit 1; }

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

# The Vault values fragment holds a live token — never leave it on disk.
cleanup() {
    [[ -n "${VAULT_VALUES_FILE}" && -f "${VAULT_VALUES_FILE}" ]] && rm -f "${VAULT_VALUES_FILE}"
    return 0
}
trap cleanup EXIT INT TERM

# ── Argument parsing ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        -f)           VALUES_FILE="$2"; shift 2 ;;
        -t)           TAG="$2";         shift 2 ;;
        -n)           NAMESPACE="$2";   shift 2 ;;
        --skip-push)  SKIP_PUSH=true;   shift ;;
        --skip-build) SKIP_BUILD=true;  shift ;;
        --skip-helm)  SKIP_HELM=true;   shift ;;
        --no-cache)   NO_CACHE=true;    shift ;;
        --vault)      USE_VAULT=true;   shift ;;
        --vault-role) VAULT_ROLE="$2";  shift 2 ;;
        --vault-path) VAULT_PATH="$2";  shift 2 ;;
        --dry-run)    DRY_RUN=true;     shift ;;
        -h|--help)    usage ;;
        *) die "Unknown option: $1" ;;
    esac
done

# ── Resolve values file ───────────────────────────────────────────────────────
if [[ -z "${VALUES_FILE}" ]]; then
    for path in "${CANDIDATE_PATHS[@]}"; do
        if [[ -f "${path}" ]]; then
            VALUES_FILE="${path}"
            break
        fi
    done
fi

if [[ "${SKIP_HELM}" == false && -z "${VALUES_FILE}" ]]; then
    die "Helm values file not found. Set SCD_VALUES_FILE, use -f FILE, or place it at:
       ${CANDIDATE_PATHS[0]}"
fi

if [[ -n "${VALUES_FILE}" && ! -f "${VALUES_FILE}" ]]; then
    die "Values file not found: ${VALUES_FILE}"
fi

# ── Resolve tag ───────────────────────────────────────────────────────────────
if [[ -z "${TAG}" ]]; then
    GIT_TAG="$(git -C "${REPO_ROOT}" describe --tags --exact-match 2>/dev/null || true)"
    TAG="${GIT_TAG:-latest}"
fi

# ── Pre-flight summary ────────────────────────────────────────────────────────
echo
echo "╔══════════════════════════════════════════════╗"
echo "║      SCD Reporting — Deploy                  ║"
echo "╚══════════════════════════════════════════════╝"
echo
info "Namespace   : ${NAMESPACE}"
info "Tag         : ${TAG}"
[[ -n "${VALUES_FILE}" ]] && info "Values file : ${VALUES_FILE}"
info "Skip push   : ${SKIP_PUSH}"
info "Skip build  : ${SKIP_BUILD}"
info "Skip helm   : ${SKIP_HELM}"
[[ "${USE_VAULT}" == true ]] && info "Vault       : fetch app token${VAULT_ROLE:+ (role ${VAULT_ROLE})}"
[[ "${DRY_RUN}" == true ]] && info "Mode        : DRY RUN — no changes will be made"
echo

# ── Step 1: git push ──────────────────────────────────────────────────────────
if [[ "${SKIP_PUSH}" == false ]]; then
    step "1 — Pushing to GitHub"
    run git -C "${REPO_ROOT}" push fnal main
    ok "Pushed to fnal/main"
else
    info "Skipping git push"
fi

# ── Step 2: Docker build & push ───────────────────────────────────────────────
if [[ "${SKIP_BUILD}" == false ]]; then
    step "2 — Building and pushing Docker image"
    BUILD_ARGS=("--push")
    [[ "${TAG}" != "latest" ]] && BUILD_ARGS+=("-t" "${TAG}")
    [[ "${NO_CACHE}" == true ]] && BUILD_ARGS+=("--no-cache")
    run "${SCRIPT_DIR}/build-docker.sh" "${BUILD_ARGS[@]}"
    ok "Image pushed"
else
    info "Skipping Docker build"
fi

# ── Step 3: Helm upgrade ──────────────────────────────────────────────────────
if [[ "${SKIP_HELM}" == false ]]; then
    HELM_VALUES_ARGS=(-f "${VALUES_FILE}")

    if [[ "${USE_VAULT}" == true ]]; then
        step "3a — Fetching Vault application token"
        TOKEN_SCRIPT="${SCRIPT_DIR}/get-vault-apptoken.sh"
        [[ -x "${TOKEN_SCRIPT}" ]] || die "Not found or not executable: ${TOKEN_SCRIPT}"

        VAULT_VALUES_FILE="$(umask 077; mktemp "${TMPDIR:-/tmp}/scd-vault-values.XXXXXX")"
        TOKEN_ARGS=(--format values -o "${VAULT_VALUES_FILE}")
        [[ -n "${VAULT_ROLE}" ]]   && TOKEN_ARGS+=(--role "${VAULT_ROLE}")
        [[ "${DRY_RUN}" == true ]] && TOKEN_ARGS+=(--dry-run)

        # Not wrapped in run(): even on a dry run we want the token script's own
        # dry-run output rather than silently skipping it.
        "${TOKEN_SCRIPT}" "${TOKEN_ARGS[@]}" || die "Could not obtain a Vault app token"

        if [[ "${DRY_RUN}" == false ]]; then
            [[ -s "${VAULT_VALUES_FILE}" ]] || die "Vault token file is empty: ${VAULT_VALUES_FILE}"
            HELM_VALUES_ARGS+=(-f "${VAULT_VALUES_FILE}")
            ok "Vault token will be passed to Helm"
        fi
        # The token fragment carries vault.addr but not the secret path. The
        # chart refuses to render with one set and not the other, so always
        # pass it alongside. --set comes after -f, so it wins over the file.
        HELM_VALUES_ARGS+=(--set "vault.secretPath=${VAULT_PATH}")
        info "Vault secrets  : ${VAULT_PATH}"
    fi

    step "3 — Running Helm upgrade"
    run helm upgrade "${RELEASE}" "${CHART}" \
        -n "${NAMESPACE}" \
        "${HELM_VALUES_ARGS[@]}"
    ok "Helm release upgraded"
else
    info "Skipping Helm upgrade"
fi

# ── Step 4: Rollout restart ───────────────────────────────────────────────────
step "4 — Restarting pod and waiting for readiness"
run oc rollout restart deployment/web -n "${NAMESPACE}"
run oc rollout status  deployment/web -n "${NAMESPACE}" --timeout=120s
echo
run oc get pods -n "${NAMESPACE}"

# ── Done ──────────────────────────────────────────────────────────────────────
echo
echo "╔══════════════════════════════════════════════╗"
echo "║      Deploy complete                         ║"
echo "╚══════════════════════════════════════════════╝"
echo
