"""TLS certificate health checks for the ``check_cert`` management command.

For each host this fetches the certificate the server presents and checks:

  connect     a TLS handshake completes at all
  trust       the chain verifies against the system CA bundle, hostname included
  name        the subjectAltName covers the host
  expiry      the certificate is valid and not inside the warning window
  revocation  OCSP, falling back to the CRL

Revocation is the reason this exists: cert-manager does not check it, so a
revoked certificate stays Ready=True until its renewal time (2026-10-19 incident,
see helm/simple/README.md). Unlike ``scripts/check-cert.sh``, which is for an
operator at a terminal, this runs unattended, so the OCSP response and the CRL
are only believed once their signatures verify against the issuing CA. An
unsigned or forged "good" must not hide a revocation.

Each check ends OK, PROBLEM, or UNKNOWN. UNKNOWN means the answer could not be
established (responder down, issuer not retrievable), not that something is
wrong; the command decides whether that alerts.

Network access goes through ``_peer_certificate``, ``_verified_handshake``,
``_http_get`` and ``_http_post`` so tests can replace them.
"""

from __future__ import annotations

import socket
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import requests
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, padding, rsa
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509 import ocsp
from cryptography.x509.oid import (
    AuthorityInformationAccessOID,
    ExtendedKeyUsageOID,
    ExtensionOID,
)

OK = 'OK'
PROBLEM = 'PROBLEM'
UNKNOWN = 'UNKNOWN'
_RANK = {OK: 0, UNKNOWN: 1, PROBLEM: 2}


@dataclass
class Check:
    name: str
    status: str
    detail: str


@dataclass
class HostReport:
    host: str
    port: int
    subject: str = ''
    issuer: str = ''
    serial: str = ''
    not_before: str = ''
    not_after: str = ''
    checks: List[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str) -> None:
        self.checks.append(Check(name, status, detail))

    @property
    def status(self) -> str:
        return worst(c.status for c in self.checks)

    def failing(self) -> List[Check]:
        return [c for c in self.checks if c.status != OK]

    def render(self) -> str:
        lines = [f'{self.host}:{self.port}  [{self.status}]']
        for label, value in (('Subject', self.subject), ('Issuer', self.issuer),
                             ('Serial', self.serial),
                             ('Valid', f'{self.not_before} -> {self.not_after}' if self.not_after else '')):
            if value:
                lines.append(f'  {label + ":":<9}{value}')
        for c in self.checks:
            lines.append(f'  {c.status:<8} {c.name:<11} {c.detail}')
        return '\n'.join(lines)


def worst(statuses) -> str:
    result = OK
    for s in statuses:
        if _RANK[s] > _RANK[result]:
            result = s
    return result


# ── Network (replaced in tests) ───────────────────────────────────────────────

def _peer_certificate(host: str, port: int, timeout: float) -> bytes:
    """DER of the leaf certificate, without verifying it."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            return tls.getpeercert(binary_form=True)


def _verified_handshake(host: str, port: int, timeout: float) -> None:
    """Raise ssl.SSLCertVerificationError if the chain or hostname does not verify."""
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host):
            pass


def _http_get(url: str, timeout: float) -> bytes:
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def _http_post(url: str, data: bytes, timeout: float) -> bytes:
    resp = requests.post(url, data=data, timeout=timeout,
                         headers={'Content-Type': 'application/ocsp-request'})
    resp.raise_for_status()
    return resp.content


# ── Certificate helpers ───────────────────────────────────────────────────────

def _utc(obj, name: str) -> Optional[datetime]:
    """``<name>_utc`` on cryptography >= 42/43, else the naive attribute as UTC."""
    value = getattr(obj, f'{name}_utc', None)
    if value is None:
        value = getattr(obj, name, None)
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
    return value


def _fmt(dt: Optional[datetime]) -> str:
    return dt.strftime('%Y-%m-%d %H:%M:%S UTC') if dt else ''


def _aia(cert: x509.Certificate, method) -> List[str]:
    try:
        aia = cert.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_INFORMATION_ACCESS).value
    except x509.ExtensionNotFound:
        return []
    return [d.access_location.value for d in aia
            if d.access_method == method and isinstance(d.access_location, x509.UniformResourceIdentifier)]


def _crl_urls(cert: x509.Certificate) -> List[str]:
    try:
        dps = cert.extensions.get_extension_for_oid(ExtensionOID.CRL_DISTRIBUTION_POINTS).value
    except x509.ExtensionNotFound:
        return []
    return [n.value for dp in dps for n in (dp.full_name or [])
            if isinstance(n, x509.UniformResourceIdentifier)]


def _issued_by(cert: x509.Certificate, issuer: x509.Certificate) -> bool:
    try:
        cert.verify_directly_issued_by(issuer)
        return True
    except (ValueError, TypeError, InvalidSignature):
        return False


def _verify_signature(public_key, signature: bytes, data: bytes, hash_alg) -> bool:
    try:
        if isinstance(public_key, rsa.RSAPublicKey):
            public_key.verify(signature, data, padding.PKCS1v15(), hash_alg)
        elif isinstance(public_key, ec.EllipticCurvePublicKey):
            public_key.verify(signature, data, ec.ECDSA(hash_alg))
        elif isinstance(public_key, (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey)):
            public_key.verify(signature, data)
        else:
            return False
        return True
    except InvalidSignature:
        return False


def _load_certs(data: bytes) -> List[x509.Certificate]:
    """Certificates from a CA Issuers download: DER, PEM, or PKCS#7 (.p7c)."""
    for loader in (lambda d: [x509.load_der_x509_certificate(d)],
                   x509.load_pem_x509_certificates,
                   pkcs7.load_der_pkcs7_certificates,
                   pkcs7.load_pem_pkcs7_certificates):
        try:
            return list(loader(data))
        except Exception:  # noqa: BLE001 — try the next encoding
            continue
    return []


def find_issuer(leaf: x509.Certificate, timeout: float) -> Tuple[Optional[x509.Certificate], str]:
    """Download the issuing CA via the AIA CA Issuers URL and confirm it signed ``leaf``."""
    urls = _aia(leaf, AuthorityInformationAccessOID.CA_ISSUERS)
    if not urls:
        return None, 'no CA Issuers URL in the certificate'
    errors = []
    for url in urls:
        try:
            for cand in _load_certs(_http_get(url, timeout)):
                if _issued_by(leaf, cand):
                    return cand, url
            errors.append(f'{url}: no certificate there signed this one')
        except Exception as exc:  # noqa: BLE001
            errors.append(f'{url}: {exc}')
    return None, '; '.join(errors)


def name_matches(cert: x509.Certificate, host: str) -> bool:
    try:
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value \
            .get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        return False
    host = host.lower().rstrip('.')
    for name in names:
        name = name.lower().rstrip('.')
        if name == host:
            return True
        # A wildcard covers exactly one leftmost label.
        if name.startswith('*.') and '.' in host and host.split('.', 1)[1] == name[2:]:
            return True
    return False


# ── Revocation ────────────────────────────────────────────────────────────────

def _reason(flag) -> str:
    return flag.name if flag is not None else 'unspecified'


def check_ocsp(leaf, issuer, timeout: float, now: datetime) -> Tuple[str, str]:
    urls = _aia(leaf, AuthorityInformationAccessOID.OCSP)
    if not urls:
        return UNKNOWN, 'no OCSP URL in the certificate'
    request = ocsp.OCSPRequestBuilder().add_certificate(leaf, issuer, hashes.SHA1()).build() \
        .public_bytes(serialization.Encoding.DER)
    errors = []
    for url in urls:
        try:
            resp = ocsp.load_der_ocsp_response(_http_post(url, request, timeout))
        except Exception as exc:  # noqa: BLE001
            errors.append(f'{url}: {exc}')
            continue
        if resp.response_status != ocsp.OCSPResponseStatus.SUCCESSFUL:
            errors.append(f'{url}: responder status {resp.response_status.name}')
            continue
        if resp.serial_number != leaf.serial_number:
            errors.append(f'{url}: response is for a different serial')
            continue

        # RFC 6960 §4.2.2.2: signed by the CA itself, or by a responder the CA
        # issued with the OCSPSigning extended key usage.
        signer = issuer
        for cand in resp.certificates:
            if cand.subject == issuer.subject and cand.public_key() == issuer.public_key():
                break
            try:
                eku = cand.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
            except x509.ExtensionNotFound:
                continue
            if ExtendedKeyUsageOID.OCSP_SIGNING in eku and _issued_by(cand, issuer):
                signer = cand
                break
        if not _verify_signature(signer.public_key(), resp.signature, resp.tbs_response_bytes,
                                 resp.signature_hash_algorithm):
            errors.append(f'{url}: response signature does not verify; ignored')
            continue

        next_update = _utc(resp, 'next_update')
        if next_update is not None and next_update < now:
            errors.append(f'{url}: response is stale (next update {_fmt(next_update)})')
            continue

        if resp.certificate_status == ocsp.OCSPCertStatus.GOOD:
            return OK, f'good (OCSP {url})'
        if resp.certificate_status == ocsp.OCSPCertStatus.REVOKED:
            return PROBLEM, (f'REVOKED at {_fmt(_utc(resp, "revocation_time"))}, '
                             f'reason {_reason(resp.revocation_reason)} (OCSP {url})')
        errors.append(f'{url}: responder does not know this certificate')
    return UNKNOWN, 'OCSP: ' + '; '.join(errors)


def check_crl(leaf, issuer, timeout: float, now: datetime) -> Tuple[str, str]:
    urls = _crl_urls(leaf)
    if not urls:
        return UNKNOWN, 'no CRL distribution point in the certificate'
    errors = []
    for url in urls:
        try:
            data = _http_get(url, timeout)
            try:
                crl = x509.load_der_x509_crl(data)
            except ValueError:
                crl = x509.load_pem_x509_crl(data)
        except Exception as exc:  # noqa: BLE001
            errors.append(f'{url}: {exc}')
            continue
        if crl.issuer != issuer.subject or not crl.is_signature_valid(issuer.public_key()):
            errors.append(f'{url}: CRL is not signed by the issuing CA; ignored')
            continue
        next_update = _utc(crl, 'next_update')
        if next_update is not None and next_update < now:
            errors.append(f'{url}: CRL is stale (next update {_fmt(next_update)})')
            continue
        entry = crl.get_revoked_certificate_by_serial_number(leaf.serial_number)
        if entry is None:
            return OK, f'good (CRL {url})'
        try:
            reason = entry.extensions.get_extension_for_class(x509.CRLReason).value.reason
        except x509.ExtensionNotFound:
            reason = None
        return PROBLEM, (f'REVOKED at {_fmt(_utc(entry, "revocation_date"))}, '
                         f'reason {_reason(reason)} (CRL {url})')
    return UNKNOWN, 'CRL: ' + '; '.join(errors)


# ── Entry point ───────────────────────────────────────────────────────────────

def parse_target(target: str) -> Tuple[str, int]:
    host, sep, port = target.strip().rpartition(':')
    if sep and port.isdigit():
        return host, int(port)
    return target.strip(), 443


def check_host(host: str, port: int = 443, warn_days: int = 21, timeout: float = 15.0,
               now: Optional[datetime] = None) -> HostReport:
    now = now or datetime.now(timezone.utc)
    report = HostReport(host=host, port=port)

    try:
        leaf = x509.load_der_x509_certificate(_peer_certificate(host, port, timeout))
    except Exception as exc:  # noqa: BLE001
        report.add('connect', PROBLEM, f'no certificate from {host}:{port}: {exc}')
        return report

    report.subject = leaf.subject.rfc4514_string()
    report.issuer = leaf.issuer.rfc4514_string()
    report.serial = format(leaf.serial_number, 'X')
    not_before, not_after = _utc(leaf, 'not_valid_before'), _utc(leaf, 'not_valid_after')
    report.not_before, report.not_after = _fmt(not_before), _fmt(not_after)

    try:
        _verified_handshake(host, port, timeout)
        report.add('trust', OK, 'chain and hostname verify against the system CA bundle')
    except ssl.SSLCertVerificationError as exc:
        report.add('trust', PROBLEM, f'does not verify: {exc.verify_message or exc}')
    except Exception as exc:  # noqa: BLE001
        report.add('trust', UNKNOWN, f'verification handshake failed: {exc}')

    if name_matches(leaf, host):
        report.add('name', OK, f'SAN covers {host}')
    else:
        report.add('name', PROBLEM, f'SAN does not cover {host} (router default certificate?)')

    days_left = (not_after - now).total_seconds() / 86400
    if not_before > now:
        report.add('expiry', PROBLEM, f'not valid until {_fmt(not_before)}')
    elif days_left < 0:
        report.add('expiry', PROBLEM, f'EXPIRED {_fmt(not_after)}')
    elif days_left < warn_days:
        report.add('expiry', PROBLEM, f'expires in {days_left:.1f} days ({_fmt(not_after)})')
    else:
        report.add('expiry', OK, f'{int(days_left)} days left')

    issuer, where = find_issuer(leaf, timeout)
    if issuer is None:
        report.add('revocation', UNKNOWN, f'issuing CA not retrievable, so OCSP/CRL cannot be checked: {where}')
        return report

    status, detail = check_ocsp(leaf, issuer, timeout, now)
    if status == UNKNOWN:
        crl_status, crl_detail = check_crl(leaf, issuer, timeout, now)
        if crl_status != UNKNOWN:
            status, detail = crl_status, f'{crl_detail}; {detail}'
        else:
            detail = f'{detail}; {crl_detail}'
    report.add('revocation', status, detail)
    return report
