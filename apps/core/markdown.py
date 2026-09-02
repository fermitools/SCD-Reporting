import xml.etree.ElementTree as etree
import markdown as md_lib
import nh3
from markdown.extensions import Extension
from markdown.inlinepatterns import InlineProcessor
from markdown.util import AtomicString


ALLOWED_TAGS = {
    'a', 'blockquote', 'br', 'code', 'em', 'h1', 'h2', 'h3', 'hr',
    'li', 'ol', 'p', 'pre', 'strong', 'table', 'tbody', 'td', 'th',
    'thead', 'tr', 'ul',
}
ALLOWED_ATTRS = {
    'a': {'href', 'title'},
    'td': {'align', 'style'},
    'th': {'align', 'style'},
}
# The only CSS python-markdown's table extension emits; everything else in a
# style attribute is dropped.
ALLOWED_STYLE_PROPERTIES = {'text-align'}
# Relative URLs are always allowed (nh3 passes them through); this list only
# constrains absolute URLs, so javascript:, data:, vbscript: etc. are dropped.
ALLOWED_URL_SCHEMES = {'http', 'https', 'mailto'}


_BARE_URL_RE = r'https?://[^\s<>]+'
_BARE_URL_TRAILING_PUNCTUATION = '.,;:!?\'"'


class _BareUrlInlineProcessor(InlineProcessor):
    """Turn bare URLs (not already part of `[text](url)` or `<url>` markup) into links."""

    def handleMatch(self, m, data):
        url = m.group(0)
        end = m.end(0)
        # Trailing punctuation is usually sentence punctuation, not part of the URL.
        # An unbalanced closing paren is treated the same way, so a URL in
        # "(see https://example.com)" doesn't swallow the closing paren.
        while url and (
            url[-1] in _BARE_URL_TRAILING_PUNCTUATION
            or (url[-1] == ')' and url.count('(') < url.count(')'))
        ):
            url = url[:-1]
            end -= 1
        if not url:
            return None, None, None
        el = etree.Element('a')
        el.set('href', url)
        el.text = AtomicString(url)
        return el, m.start(0), end


class _AutolinkExtension(Extension):
    def extendMarkdown(self, md):
        # Priority 75 runs after 'link'/'autolink'/'html' (which claim URLs already
        # wrapped in markdown or HTML link syntax) but before emphasis patterns.
        md.inlinePatterns.register(_BareUrlInlineProcessor(_BARE_URL_RE, md), 'autolink_bare_url', 75)


def render_markdown(text: str) -> str:
    html = md_lib.markdown(text or '', extensions=['fenced_code', 'tables', _AutolinkExtension()])
    # nh3 (Rust "ammonia" bindings) parses with a real HTML5 tree builder, so the
    # output is what a browser would see; the previous html.parser-based
    # allowlist was vulnerable to parser-differential bypasses (GitHub #10).
    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        url_schemes=ALLOWED_URL_SCHEMES,
        filter_style_properties=ALLOWED_STYLE_PROPERTIES,
        link_rel='noopener noreferrer',
        strip_comments=True,
    )
