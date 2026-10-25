"""Tests for the TLS certificate monitor (apps/core/certmonitor.py, check_cert).

Everything runs offline: a throwaway CA, leaf, OCSP responses and CRLs are built
with ``cryptography``, and the module's network functions are monkeypatched.
"""

from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509 import ocsp
from cryptography.x509.oid import AuthorityInformationAccessOID, ExtendedKeyUsageOID, NameOID
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.core import certmonitor as cm

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
HOST = 'scd-reporting.fnal.gov'
OCSP_URL = 'http://ocsp.test'
CRL_URL = 'http://crl.test/ca.crl'
AIA_URL = 'http://ca.test/ca.crt'


# ── PKI builders ──────────────────────────────────────────────────────────────

def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _ca(cn='Test CA', key=None):
    key = key or ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder()
            .subject_name(_name(cn)).issuer_name(_name(cn))
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(NOW - timedelta(days=365)).not_valid_after(NOW + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return cert, key


def _leaf(ca, ca_key, names=(HOST,), not_before=None, not_after=None, aia=True, crl=True):
    key = ec.generate_private_key(ec.SECP256R1())
    b = (x509.CertificateBuilder()
         .subject_name(_name(names[0])).issuer_name(ca.subject)
         .public_key(key.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(not_before or NOW - timedelta(days=10))
         .not_valid_after(not_after or NOW + timedelta(days=180))
         .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False))
    if aia:
        b = b.add_extension(x509.AuthorityInformationAccess([
            x509.AccessDescription(AuthorityInformationAccessOID.OCSP, x509.UniformResourceIdentifier(OCSP_URL)),
            x509.AccessDescription(AuthorityInformationAccessOID.CA_ISSUERS, x509.UniformResourceIdentifier(AIA_URL)),
        ]), critical=False)
    if crl:
        b = b.add_extension(x509.CRLDistributionPoints([
            x509.DistributionPoint([x509.UniformResourceIdentifier(CRL_URL)], None, None, None),
        ]), critical=False)
    return b.sign(ca_key, hashes.SHA256())


def _ocsp(leaf, ca, signer_key, status=ocsp.OCSPCertStatus.GOOD, reason=None,
          next_update=None, responder=None, include=()):
    b = ocsp.OCSPResponseBuilder().add_response(
        cert=leaf, issuer=ca, algorithm=hashes.SHA1(), cert_status=status,
        this_update=NOW - timedelta(hours=1),
        next_update=next_update or NOW + timedelta(days=1),
        revocation_time=NOW - timedelta(days=1) if status == ocsp.OCSPCertStatus.REVOKED else None,
        revocation_reason=reason if status == ocsp.OCSPCertStatus.REVOKED else None,
    ).responder_id(ocsp.OCSPResponderEncoding.HASH, responder or ca)
    if include:
        b = b.certificates(list(include))
    return b.sign(signer_key, hashes.SHA256()).public_bytes(serialization.Encoding.DER)


def _crl(ca, ca_key, revoked=(), next_update=None):
    next_update = next_update or NOW + timedelta(days=7)
    b = (x509.CertificateRevocationListBuilder().issuer_name(ca.subject)
         .last_update(min(NOW, next_update) - timedelta(days=7)).next_update(next_update))
    for serial in revoked:
        b = b.add_revoked_certificate(
            x509.RevokedCertificateBuilder().serial_number(serial).revocation_date(NOW - timedelta(days=1))
            .add_extension(x509.CRLReason(x509.ReasonFlags.superseded), critical=False).build())
    return b.sign(ca_key, hashes.SHA256()).public_bytes(serialization.Encoding.DER)


def _der(cert):
    return cert.public_bytes(serialization.Encoding.DER)


@pytest.fixture
def pki():
    ca, ca_key = _ca()
    return ca, ca_key, _leaf(ca, ca_key)


@pytest.fixture
def net(monkeypatch):
    """Route the module's network calls to in-memory answers set by each test."""
    state = {'peer': None, 'trust_error': None, 'get': {}, 'post': {}}

    def peer(host, port, timeout):
        if isinstance(state['peer'], Exception):
            raise state['peer']
        return state['peer']

    def handshake(host, port, timeout):
        if state['trust_error']:
            raise state['trust_error']

    def answer(table, url):
        value = table.get(url, ConnectionError(f'unreachable: {url}'))
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(cm, '_peer_certificate', peer)
    monkeypatch.setattr(cm, '_verified_handshake', handshake)
    monkeypatch.setattr(cm, '_http_get', lambda url, timeout: answer(state['get'], url))
    monkeypatch.setattr(cm, '_http_post', lambda url, data, timeout: answer(state['post'], url))
    return state


def _serve(net, ca, leaf, ocsp_body=None, crl_body=None):
    net['peer'] = _der(leaf)
    net['get'][AIA_URL] = _der(ca)
    if ocsp_body is not None:
        net['post'][OCSP_URL] = ocsp_body
    if crl_body is not None:
        net['get'][CRL_URL] = crl_body


def _check(name, report):
    return next(c for c in report.checks if c.name == name)


# ── Revocation ────────────────────────────────────────────────────────────────

def test_good_by_ocsp(net, pki):
    ca, key, leaf = pki
    _serve(net, ca, leaf, ocsp_body=_ocsp(leaf, ca, key))
    report = cm.check_host(HOST, now=NOW)
    assert report.status == cm.OK
    assert 'OCSP' in _check('revocation', report).detail


def test_revoked_by_ocsp_reports_reason(net, pki):
    ca, key, leaf = pki
    _serve(net, ca, leaf, ocsp_body=_ocsp(leaf, ca, key, ocsp.OCSPCertStatus.REVOKED,
                                          reason=x509.ReasonFlags.superseded))
    report = cm.check_host(HOST, now=NOW)
    rev = _check('revocation', report)
    assert report.status == cm.PROBLEM and rev.status == cm.PROBLEM
    assert 'REVOKED' in rev.detail and 'superseded' in rev.detail


def test_rsa_signed_ocsp_is_verified(net):
    ca, key = _ca(key=rsa.generate_private_key(public_exponent=65537, key_size=2048))
    leaf = _leaf(ca, key)
    _serve(net, ca, leaf, ocsp_body=_ocsp(leaf, ca, key, ocsp.OCSPCertStatus.REVOKED))
    assert _check('revocation', cm.check_host(HOST, now=NOW)).status == cm.PROBLEM


def test_forged_ocsp_good_is_ignored_and_crl_wins(net, pki):
    """A 'good' signed by some other key must not mask a revocation."""
    ca, key, leaf = pki
    # An impostor CA with the real CA's name but its own key.
    impostor, impostor_key = _ca(ca.subject.rfc4514_string().split('=', 1)[1])
    _serve(net, ca, leaf, ocsp_body=_ocsp(leaf, ca, impostor_key, responder=impostor),
           crl_body=_crl(ca, key, revoked=[leaf.serial_number]))
    rev = _check('revocation', cm.check_host(HOST, now=NOW))
    assert rev.status == cm.PROBLEM
    assert 'CRL' in rev.detail and 'signature does not verify' in rev.detail


def test_delegated_ocsp_responder_is_accepted(net, pki):
    ca, key, leaf = pki
    rkey = ec.generate_private_key(ec.SECP256R1())
    responder = (x509.CertificateBuilder()
                 .subject_name(_name('OCSP responder')).issuer_name(ca.subject)
                 .public_key(rkey.public_key()).serial_number(x509.random_serial_number())
                 .not_valid_before(NOW - timedelta(days=1)).not_valid_after(NOW + timedelta(days=30))
                 .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.OCSP_SIGNING]), critical=False)
                 .sign(key, hashes.SHA256()))
    _serve(net, ca, leaf, ocsp_body=_ocsp(leaf, ca, rkey, responder=responder, include=[responder]))
    assert _check('revocation', cm.check_host(HOST, now=NOW)).status == cm.OK


def test_delegated_responder_without_ocsp_signing_eku_is_rejected(net, pki):
    ca, key, leaf = pki
    rkey = ec.generate_private_key(ec.SECP256R1())
    responder = (x509.CertificateBuilder()
                 .subject_name(_name('Not a responder')).issuer_name(ca.subject)
                 .public_key(rkey.public_key()).serial_number(x509.random_serial_number())
                 .not_valid_before(NOW - timedelta(days=1)).not_valid_after(NOW + timedelta(days=30))
                 .sign(key, hashes.SHA256()))
    _serve(net, ca, leaf, ocsp_body=_ocsp(leaf, ca, rkey, responder=responder, include=[responder]))
    assert _check('revocation', cm.check_host(HOST, now=NOW)).status == cm.UNKNOWN


def test_stale_ocsp_falls_back_to_crl(net, pki):
    ca, key, leaf = pki
    _serve(net, ca, leaf, ocsp_body=_ocsp(leaf, ca, key, next_update=NOW - timedelta(hours=2)),
           crl_body=_crl(ca, key))
    rev = _check('revocation', cm.check_host(HOST, now=NOW))
    assert rev.status == cm.OK and 'CRL' in rev.detail and 'stale' in rev.detail


def test_ocsp_down_crl_revoked(net, pki):
    ca, key, leaf = pki
    _serve(net, ca, leaf, crl_body=_crl(ca, key, revoked=[leaf.serial_number]))
    rev = _check('revocation', cm.check_host(HOST, now=NOW))
    assert rev.status == cm.PROBLEM and 'superseded' in rev.detail


def test_crl_from_another_ca_is_ignored(net, pki):
    ca, key, leaf = pki
    other, other_key = _ca('Other CA')
    _serve(net, ca, leaf, crl_body=_crl(other, other_key))
    assert _check('revocation', cm.check_host(HOST, now=NOW)).status == cm.UNKNOWN


def test_stale_crl_is_unknown(net, pki):
    ca, key, leaf = pki
    _serve(net, ca, leaf, crl_body=_crl(ca, key, next_update=NOW - timedelta(days=1)))
    assert _check('revocation', cm.check_host(HOST, now=NOW)).status == cm.UNKNOWN


def test_both_sources_down_is_unknown(net, pki):
    ca, key, leaf = pki
    _serve(net, ca, leaf)
    report = cm.check_host(HOST, now=NOW)
    assert report.status == cm.UNKNOWN


def test_issuer_not_retrievable_is_unknown(net, pki):
    ca, key, leaf = pki
    net['peer'] = _der(leaf)
    assert 'issuing CA not retrievable' in _check('revocation', cm.check_host(HOST, now=NOW)).detail


def test_issuer_url_serving_wrong_ca_is_unknown(net, pki):
    ca, key, leaf = pki
    other, _ = _ca('Other CA')
    net['peer'] = _der(leaf)
    net['get'][AIA_URL] = _der(other)
    assert _check('revocation', cm.check_host(HOST, now=NOW)).status == cm.UNKNOWN


def test_issuer_as_pem(net, pki):
    ca, key, leaf = pki
    _serve(net, ca, leaf, ocsp_body=_ocsp(leaf, ca, key))
    net['get'][AIA_URL] = ca.public_bytes(serialization.Encoding.PEM)
    assert _check('revocation', cm.check_host(HOST, now=NOW)).status == cm.OK


# ── Connection, trust, name, expiry ───────────────────────────────────────────

def test_connect_failure_is_problem(net):
    net['peer'] = ConnectionRefusedError('refused')
    report = cm.check_host(HOST, now=NOW)
    assert report.status == cm.PROBLEM and report.checks[0].name == 'connect'


def test_untrusted_chain_is_problem(net, pki):
    import ssl
    ca, key, leaf = pki
    _serve(net, ca, leaf, ocsp_body=_ocsp(leaf, ca, key))
    err = ssl.SSLCertVerificationError('verify failed')
    err.verify_message = 'unable to get local issuer certificate'
    net['trust_error'] = err
    trust = _check('trust', cm.check_host(HOST, now=NOW))
    assert trust.status == cm.PROBLEM and 'local issuer' in trust.detail


def test_name_mismatch_is_problem(net):
    ca, key = _ca()
    leaf = _leaf(ca, key, names=('other.fnal.gov',))
    _serve(net, ca, leaf, ocsp_body=_ocsp(leaf, ca, key))
    assert _check('name', cm.check_host(HOST, now=NOW)).status == cm.PROBLEM


@pytest.mark.parametrize('days,status', [(30, cm.OK), (5, cm.PROBLEM), (-1, cm.PROBLEM)])
def test_expiry_window(net, days, status):
    ca, key = _ca()
    leaf = _leaf(ca, key, not_before=NOW - timedelta(days=100), not_after=NOW + timedelta(days=days))
    _serve(net, ca, leaf, ocsp_body=_ocsp(leaf, ca, key))
    assert _check('expiry', cm.check_host(HOST, warn_days=21, now=NOW)).status == status


@pytest.mark.parametrize('san,host,ok', [
    ('scd-reporting.fnal.gov', 'SCD-Reporting.fnal.gov', True),
    ('*.fnal.gov', 'scd-reporting.fnal.gov', True),
    ('*.fnal.gov', 'a.b.fnal.gov', False),
    ('*.fnal.gov', 'fnal.gov', False),
    ('other.fnal.gov', 'scd-reporting.fnal.gov', False),
])
def test_name_matching(san, host, ok):
    ca, key = _ca()
    assert cm.name_matches(_leaf(ca, key, names=(san,)), host) is ok


@pytest.mark.parametrize('target,expected', [
    ('scd-reporting.fnal.gov', ('scd-reporting.fnal.gov', 443)),
    ('scd-reporting.fnal.gov:8443', ('scd-reporting.fnal.gov', 8443)),
    (' host ', ('host', 443)),
])
def test_parse_target(target, expected):
    assert cm.parse_target(target) == expected


# ── Command ───────────────────────────────────────────────────────────────────

def _report(status, host=HOST):
    r = cm.HostReport(host=host, port=443, serial='AB')
    r.add('revocation', status, {cm.OK: 'good (OCSP x)', cm.PROBLEM: 'REVOKED at t, reason superseded (OCSP x)',
                                 cm.UNKNOWN: 'OCSP: down; CRL: down'}[status])
    return r


@pytest.fixture
def canned(monkeypatch, settings):
    settings.CERT_MONITOR_HOSTS = [HOST]
    settings.CERT_MONITOR_RECIPIENTS = ['ops@example.org']
    settings.CERT_MONITOR_ALERT_ON_UNKNOWN = True
    state = {'status': cm.OK}
    monkeypatch.setattr(cm, 'check_host', lambda host, port, **kw: _report(state['status'], host))
    return state


def test_command_ok_sends_nothing(canned):
    call_command('check_cert')
    assert mail.outbox == []


def test_command_problem_mails_and_exits_1(canned):
    canned['status'] = cm.PROBLEM
    with pytest.raises(CommandError) as exc:
        call_command('check_cert')
    assert exc.value.returncode == 1
    assert len(mail.outbox) == 1
    msg = mail.outbox[0]
    assert msg.to == ['ops@example.org']
    assert HOST in msg.subject and 'PROBLEM' in msg.subject and 'superseded' in msg.subject
    assert 'check-cert.sh --reissue' in msg.body


def test_command_cli_recipients_override_settings(canned):
    canned['status'] = cm.PROBLEM
    with pytest.raises(CommandError):
        call_command('check_cert', '--to', 'a@example.org', '--to', 'b@example.org')
    assert mail.outbox[0].to == ['a@example.org', 'b@example.org']


def test_command_problem_without_recipients_exits_2(canned, settings):
    settings.CERT_MONITOR_RECIPIENTS = []
    canned['status'] = cm.PROBLEM
    with pytest.raises(CommandError) as exc:
        call_command('check_cert')
    assert exc.value.returncode == 2


def test_command_smtp_failure_exits_2(canned, monkeypatch):
    canned['status'] = cm.PROBLEM

    def boom(self, fail_silently=False):
        raise OSError('connection refused')

    monkeypatch.setattr('django.core.mail.EmailMessage.send', boom)
    with pytest.raises(CommandError) as exc:
        call_command('check_cert')
    assert exc.value.returncode == 2


def test_command_unknown_alerts_by_default(canned):
    canned['status'] = cm.UNKNOWN
    with pytest.raises(CommandError):
        call_command('check_cert')
    assert 'UNKNOWN' in mail.outbox[0].subject


def test_command_unknown_ignored_when_asked(canned):
    canned['status'] = cm.UNKNOWN
    call_command('check_cert', '--ignore-unknown')
    assert mail.outbox == []


def test_command_send_test_mails_when_ok(canned):
    call_command('check_cert', '--send-test')
    assert len(mail.outbox) == 1 and 'all OK' in mail.outbox[0].subject


def test_command_dry_run_and_no_email_send_nothing(canned):
    canned['status'] = cm.PROBLEM
    for flag in ('--dry-run', '--no-email'):
        with pytest.raises(CommandError):
            call_command('check_cert', flag)
    assert mail.outbox == []


def test_command_without_hosts_exits_2(canned, settings):
    settings.CERT_MONITOR_HOSTS = []
    with pytest.raises(CommandError) as exc:
        call_command('check_cert')
    assert exc.value.returncode == 2


def test_csv_setting_parser():
    from scd_reporting.settings.base import _csv
    assert _csv('a@x.org, b@x.org,,') == ['a@x.org', 'b@x.org']
    assert _csv(['a@x.org', ' b@x.org ']) == ['a@x.org', 'b@x.org']
    assert _csv('') == []
