"""Generate an AI-written narrative summary of a WorkItem queryset via the Anthropic API."""
import logging
import os
from typing import NamedTuple

from django.conf import settings

logger = logging.getLogger(__name__)

from .exporters import _rows


def _format_entries(qs) -> str:
    parts = []
    for r in _rows(qs):
        flags = []
        if r['critical'] == 'yes':
            flags.append('CRITICAL')
        if r['highlight'] == 'yes':
            stars = '★' * int(r['highlight_stars']) if r['highlight_stars'] else ''
            flags.append(f'HIGHLIGHT {stars}'.strip())
        if r['private'] == 'yes':
            flags.append('PRIVATE')
        flag_str = f'  [{", ".join(flags)}]' if flags else ''
        group_str = f'  |  group: {r["group"]}' if r['group'] else ''
        tags_str = f'  |  tags: {r["tags"]}' if r['tags'] else ''
        desc_str = f'\n    {r["description"]}' if r['description'] else ''
        parts.append(
            f'- [{r["period_start"]} – {r["period_end"]}] {r["title"]}{flag_str}\n'
            f'  Author: {r["author"]}  |  {r["project"]} / {r["category"]}'
            + group_str
            + tags_str
            + desc_str
        )
    return '\n'.join(parts) if parts else '(no entries)'


class SummaryResult(NamedTuple):
    """The generated summary plus enough metadata to tell a partial one apart.

    ``truncated`` is the reason the summary used to stop mid-section without
    saying so: the model hit ``max_tokens`` and the caller returned the partial
    text as though it were complete.
    """

    text: str
    truncated: bool
    stop_reason: str
    input_tokens: int
    output_tokens: int
    max_tokens: int


def _prepare(qs, user=None, template=None, max_tokens=None):
    """Build everything a summary request needs, shared by both code paths.

    Returns ``(client, model, system_prompt, messages, max_tokens)``. Raises
    ValueError when the API key is missing or the prompt is too large.
    """
    import anthropic
    from .models import AIPromptConfig

    api_key = getattr(settings, 'ANTHROPIC_API_KEY', '') or os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        raise ValueError(
            'ANTHROPIC_API_KEY is not configured. '
            'Set it as an environment variable or in Django settings.'
        )

    if template is not None:
        system_prompt = template.system_prompt
        user_template = template.user_template
    else:
        config = AIPromptConfig.for_user(user) if user is not None else AIPromptConfig.get_solo()
        system_prompt = config.system_prompt
        user_template = config.user_template

    model = settings.ANTHROPIC_SUMMARY_MODEL
    if max_tokens is None:
        max_tokens = int(settings.ANTHROPIC_MAX_TOKENS)
    base_url = settings.ANTHROPIC_BASE_URL or None
    client = anthropic.Anthropic(api_key=api_key, **({"base_url": base_url} if base_url else {}))

    messages = [
        {'role': 'user', 'content': user_template.format(entries=_format_entries(qs))}
    ]

    # Refuse an oversized request with an actionable message rather than
    # silently truncating the entries or letting the API reject the call.
    #
    # The pre-count is approximate, not a budget. Measured against the lab's
    # LiteLLM proxy, count_tokens reported ~325k for a request the API then
    # billed at ~480k input tokens, so the check is deliberately loose: it is
    # here to catch a runaway input with a readable error, not to police spend.
    limit = int(settings.ANTHROPIC_MAX_INPUT_TOKENS)
    if limit > 0:
        try:
            counted = client.messages.count_tokens(
                model=model, system=system_prompt, messages=messages,
            ).input_tokens
        except Exception as exc:  # noqa: BLE001 — a failed pre-count must not block the summary
            logger.warning('ai_summary: token pre-count failed: %s', exc)
            counted = 0
        if counted > limit:
            raise ValueError(
                f'These {qs.count()} entries come to about {counted:,} input tokens, over the '
                f'{limit:,} configured limit. Narrow the filters, select fewer rows in the '
                'preview, or raise ANTHROPIC_MAX_INPUT_TOKENS.'
            )

    return client, model, system_prompt, messages, int(max_tokens)


def _result_from(message, max_tokens) -> SummaryResult:
    text = '\n'.join(block.text for block in message.content if block.type == 'text')
    truncated = message.stop_reason == 'max_tokens'
    if truncated:
        logger.warning(
            'ai_summary: response hit max_tokens=%s (%s output tokens) — summary is partial',
            max_tokens, message.usage.output_tokens,
        )
    return SummaryResult(
        text=text,
        truncated=truncated,
        stop_reason=message.stop_reason or '',
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
        max_tokens=max_tokens,
    )


def generate(qs, user=None, template=None) -> SummaryResult:
    """Call the Anthropic API and return the summary as Markdown plus metadata.

    template: a NamedPromptTemplate instance; overrides the user's default config when provided.
    """
    client, model, system_prompt, messages, max_tokens = _prepare(qs, user, template)

    # Streamed so a long summary cannot trip the SDK's HTTP timeout, which is
    # what a large max_tokens on a blocking request risks.
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=messages,
    ) as stream:
        message = stream.get_final_message()

    return _result_from(message, max_tokens)


def generate_stream(qs, user=None, template=None, max_tokens=None):
    """Yield the summary incrementally, then the finished SummaryResult.

    Yields ``('delta', text)`` for each chunk the model emits and finally
    ``('done', SummaryResult)``. Lets the caller push text to the browser as it
    arrives, which is what takes the generation out from under a single
    blocking request — the ceiling on a non-streamed summary is the 300s
    gunicorn/route timeout, not the model.
    """
    if max_tokens is None:
        max_tokens = int(getattr(settings, 'ANTHROPIC_STREAM_MAX_TOKENS', 0)
                         or settings.ANTHROPIC_MAX_TOKENS)
    client, model, system_prompt, messages, max_tokens = _prepare(
        qs, user, template, max_tokens=max_tokens,
    )

    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=messages,
    ) as stream:
        for chunk in stream.text_stream:
            if chunk:
                yield 'delta', chunk
        message = stream.get_final_message()

    yield 'done', _result_from(message, max_tokens)
