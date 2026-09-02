"""Tests for core views: bug report submission (rate limiting, validation, auth)."""
import pytest

from django.core.cache import cache
from django.urls import reverse

from apps.accounts.models import User


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def user(db):
    return User.objects.create_user(username='tester', email='tester@example.com', password='pass')


class TestBugReportSubmit:
    def test_anonymous_redirected(self, client):
        resp = client.post(reverse('bug-report-submit'), {'title': 'x', 'body': 'y'})
        assert resp.status_code == 302
        assert '/login' in resp['Location'] or '/accounts' in resp['Location']

    def test_missing_title_rejected(self, client, user):
        client.force_login(user)
        resp = client.post(reverse('bug-report-submit'), {'title': '', 'body': 'some body'})
        assert resp.status_code == 302
        assert resp['Location'].endswith(reverse('bug-report'))

    def test_missing_body_rejected(self, client, user):
        client.force_login(user)
        resp = client.post(reverse('bug-report-submit'), {'title': 'A title', 'body': ''})
        assert resp.status_code == 302
        assert resp['Location'].endswith(reverse('bug-report'))

    def test_rate_limit_blocks_after_threshold(self, client, user, monkeypatch):
        submitted = []

        def fake_post(url, json, headers, timeout):
            submitted.append(json['title'])
            class R:
                status_code = 201
                def json(self): return {'number': len(submitted)}
            return R()

        monkeypatch.setattr('apps.core.views._resolve_github_token', lambda: 'fake-token')
        import requests as http_requests
        monkeypatch.setattr(http_requests, 'post', fake_post)

        client.force_login(user)
        for i in range(3):
            resp = client.post(reverse('bug-report-submit'), {'title': f'Bug {i}', 'body': 'desc'})
            assert resp.status_code == 302
            assert resp['Location'].endswith(reverse('about'))

        # Fourth submission should be rate-limited
        resp = client.post(reverse('bug-report-submit'), {'title': 'Bug 4', 'body': 'desc'})
        assert resp.status_code == 302
        assert resp['Location'].endswith(reverse('bug-report'))
        assert len(submitted) == 3  # GitHub API not called a 4th time

    def test_github_failure_shows_generic_message(self, client, user, monkeypatch):
        def fake_post(url, json, headers, timeout):
            class R:
                status_code = 500
                text = 'Internal Server Error'
                def json(self): return {'message': 'Server Error'}
            return R()

        monkeypatch.setattr('apps.core.views._resolve_github_token', lambda: 'fake-token')
        import requests as http_requests
        monkeypatch.setattr(http_requests, 'post', fake_post)

        client.force_login(user)
        resp = client.post(reverse('bug-report-submit'), {'title': 'Bug', 'body': 'desc'},
                           follow=True)
        content = resp.content.decode()
        assert 'Server Error' not in content
        assert 'could not be submitted' in content

    def test_no_token_shows_generic_message(self, client, user, monkeypatch):
        monkeypatch.setattr('apps.core.views._resolve_github_token', lambda: None)

        client.force_login(user)
        resp = client.post(reverse('bug-report-submit'), {'title': 'Bug', 'body': 'desc'},
                           follow=True)
        assert 'not configured' in resp.content.decode()


# ── Markdown sanitizer (issue #10) ────────────────────────────────────────────

class TestRenderMarkdown:
    """render_markdown must strip every scripting vector a browser would honour."""

    @pytest.mark.parametrize('payload', [
        '<script>alert(1)</script>',
        '<img src=x onerror=alert(1)>',
        '<svg/onload=alert(1)>',
        '<iframe src="https://evil.example"></iframe>',
        '<p onclick="alert(1)">t</p>',
        '<a href="javascript:alert(1)">x</a>',
        '<a href="JaVaScRiPt:alert(1)">x</a>',
        '<a href="  javascript:alert(1)">x</a>',
        '<a href="&#106;avascript:alert(1)">x</a>',
        '<a href="java&#x09;script:alert(1)">x</a>',
        '<a href="data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==">x</a>',
        '<a href="vbscript:msgbox(1)">x</a>',
        '[x](javascript:alert(1))',
        '<scr<script>ipt>alert(1)</script>',
        '<a href="https://ok.example" onmouseover=alert(1)//>x</a>',
        '<style>body{background:url(javascript:alert(1))}</style>',
        '<math><mtext><table><mglyph><style><img src=x onerror=alert(1)>',
        '<!--<img src=x onerror=alert(1)>-->',
    ])
    def test_blocks_xss_vectors(self, payload):
        from apps.core.markdown import render_markdown
        out = render_markdown(payload).lower()
        for needle in ('<script', 'onerror', 'onload', 'onclick', 'onmouseover',
                       'javascript:', 'vbscript:', 'data:', '<iframe', '<svg', '<style', '<!--'):
            assert needle not in out, f'{needle!r} survived in {out!r}'

    def test_keeps_allowed_markup(self):
        from apps.core.markdown import render_markdown
        out = render_markdown(
            '# Head\n\n**bold** and *em* with `code` and [link](https://example.com "t") '
            'plus [mail](mailto:a@b.example) and [rel](/entries/)\n\n'
            '| Col | Num |\n|:----|----:|\n| a | 1 |\n\n```\nraw <b>\n```\n\n---\n'
        )
        assert '<h1>Head</h1>' in out
        assert '<strong>bold</strong>' in out and '<em>em</em>' in out
        assert '<code>code</code>' in out
        assert 'href="https://example.com"' in out and 'title="t"' in out
        assert 'href="mailto:a@b.example"' in out
        assert 'href="/entries/"' in out
        assert '<table>' in out and 'text-align:right' in out
        assert '<pre><code>raw &lt;b&gt;' in out
        assert '<hr>' in out

    def test_links_get_noopener(self):
        from apps.core.markdown import render_markdown
        out = render_markdown('[x](https://example.com)')
        assert 'rel="noopener noreferrer"' in out

    def test_bare_url_autolinked_and_sanitized(self):
        from apps.core.markdown import render_markdown
        out = render_markdown('see https://example.com/a?b=1.')
        assert '<a href="https://example.com/a?b=1"' in out
        assert 'javascript' not in render_markdown('see javascript:alert(1)').lower() or \
               '<a' not in render_markdown('see javascript:alert(1)')

    def test_empty_and_none(self):
        from apps.core.markdown import render_markdown
        assert render_markdown('') == ''
        assert render_markdown(None) == ''
